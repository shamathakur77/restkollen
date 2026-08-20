"""Fetch the current medicine-shortage XML from Läkemedelsverket.

Runs on GitHub Actions runners (this data source is not reachable from
all environments; the pipeline is designed so every external fetch
happens here).

Strategy (fail closed at every step):
  1. Try the cached download URL (data/source_url.txt, committed).
  2. On failure, re-resolve the URL from Sveriges dataportal DCAT
     metadata -> Läkemedelsverket catalog distribution metadata.
  3. As a last resort, try the last-known hardcoded URL.
Each URL is tried with two header profiles: a polite identifying one,
then a browser-like one (docetp.mpa.se's WAF answers 406 Not Acceptable
to non-browser requests). Retries with backoff, loud logging of
response codes, gzip handling. The downloaded XML is validated
(namespace, root element, minimum record count) before it is handed to
the build step.
Exit codes: 0 = ok, 2 = fetch/validation failed (do not publish).
"""

from __future__ import annotations

import gzip
import io
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from common import (
    DATAPORTAL_METADATA_URL,
    LAST_KNOWN_DOWNLOAD_URL,
    MIN_RECORDS,
    NS_PREFIX,
    USER_AGENT,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = REPO_ROOT / "data" / "source_url.txt"
OUT_FILE = REPO_ROOT / "work" / "current.xml"

RETRIES = 2
BACKOFF_SECONDS = [5, 15]
TIMEOUT = 90

# Header profiles, tried in order. Profile 2 exists because the source
# host rejects obviously non-browser requests with HTTP 406.
HEADER_PROFILES = [
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml, text/xml;q=0.9, */*;q=0.8",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, identity",
    },
]


def log(msg: str) -> None:
    print(f"[fetch] {msg}", flush=True)


def _read_body(resp) -> bytes:
    body = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
    return body


def http_get(url: str, headers: dict, label: str = "") -> bytes:
    """GET with retries + backoff for one header profile. Raises on
    final failure."""
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                status = getattr(resp, "status", 200)
                body = _read_body(resp)
                log(f"GET {url} {label}-> HTTP {status}, {len(body)} bytes (attempt {attempt})")
                return body
        except urllib.error.HTTPError as e:
            last_err = e
            log(f"GET {url} {label}-> HTTP {e.code} {e.reason} (attempt {attempt})")
        except Exception as e:  # URLError, timeout, ...
            last_err = e
            log(f"GET {url} {label}-> {type(e).__name__}: {e} (attempt {attempt})")
        if attempt < RETRIES:
            wait = BACKOFF_SECONDS[attempt - 1]
            log(f"retrying in {wait}s ...")
            time.sleep(wait)
    raise RuntimeError(f"attempts exhausted for {url}: {last_err}")


def http_get_any_profile(url: str) -> bytes:
    """GET trying each header profile in turn."""
    last_err: Exception | None = None
    for i, headers in enumerate(HEADER_PROFILES, 1):
        try:
            return http_get(url, headers, label=f"[profile {i}] ")
        except Exception as e:
            last_err = e
            log(f"profile {i} failed for {url}: {e}")
    raise RuntimeError(f"all header profiles failed for {url}: {last_err}")


def resolve_download_url() -> str | None:
    """Resolve the XML download URL from dataportal DCAT metadata.

    Dataset metadata (RDF/XML) references distribution resources at
    catalog.lakemedelsverket.se; each distribution's own metadata holds
    dcat:downloadURL. We pick the application/xml distribution whose
    URL contains 'current'.
    """
    try:
        rdf = http_get_any_profile(DATAPORTAL_METADATA_URL).decode("utf-8", "replace")
    except Exception as e:
        log(f"dataportal metadata fetch failed: {e}")
        return None

    dist_urls = re.findall(
        r"https://catalog\.lakemedelsverket\.se/store/\d+/resource/\d+", rdf
    )
    dist_urls = list(dict.fromkeys(dist_urls))  # dedupe, keep order
    log(f"dataportal lists {len(dist_urls)} distribution resource(s)")

    candidates: list[str] = []
    for res_url in dist_urls:
        meta_url = res_url.replace("/resource/", "/metadata/")
        try:
            meta = http_get_any_profile(meta_url).decode("utf-8", "replace")
        except Exception as e:
            log(f"distribution metadata fetch failed ({meta_url}): {e}")
            continue
        # downloadURL appears as rdf:resource attribute or element text.
        for m in re.findall(r'downloadURL[^>]*?resource="([^"]+)"', meta):
            candidates.append(m)
        for m in re.findall(r"downloadURL[^>]*>\s*([^<\s]+)\s*<", meta):
            candidates.append(m)

    candidates = [c for c in dict.fromkeys(candidates) if c.lower().endswith(".xml")]
    log(f"candidate XML download URLs: {candidates}")
    current = [c for c in candidates if "current" in c.lower()]
    if current:
        return current[0]
    if candidates:
        return candidates[0]
    return None


def validate_xml(body: bytes, min_records: int = MIN_RECORDS) -> int:
    """Validate schema shape. Returns record count. Raises on problems."""
    root = ET.fromstring(body)  # raises ParseError on malformed XML
    if not root.tag.startswith("{" + NS_PREFIX):
        raise ValueError(
            f"unexpected root namespace/tag: {root.tag!r} "
            f"(expected namespace starting with {NS_PREFIX!r})"
        )
    if not root.tag.endswith("}OpenDataMedicineShortages"):
        raise ValueError(f"unexpected root element: {root.tag!r}")
    if "creationDate" not in root.attrib:
        raise ValueError("root element lacks creationDate attribute")
    ns = root.tag[1 : root.tag.index("}")]
    records = root.findall(f".//{{{ns}}}MedicineShortage")
    if len(records) < min_records:
        raise ValueError(
            f"only {len(records)} MedicineShortage records "
            f"(minimum {min_records}). refusing to publish"
        )
    return len(records)


def try_url(url: str) -> bytes | None:
    try:
        body = http_get_any_profile(url)
        n = validate_xml(body)
        log(f"OK: {url} validated with {n} records")
        return body
    except Exception as e:
        log(f"FAILED for {url}: {type(e).__name__}: {e}")
        return None


def main() -> int:
    cached = None
    if CACHE_FILE.exists():
        cached = CACHE_FILE.read_text().strip() or None

    tried: list[str] = []
    body = None
    used_url = None

    for url in [cached, "RESOLVE", LAST_KNOWN_DOWNLOAD_URL]:
        if url is None:
            continue
        if url == "RESOLVE":
            log("resolving download URL from Sveriges dataportal ...")
            url = resolve_download_url()
            if url is None:
                log("resolution produced no URL")
                continue
            log(f"resolved download URL: {url}")
        if url in tried:
            log(f"already tried {url}, skipping")
            continue
        tried.append(url)
        body = try_url(url)
        if body is not None:
            used_url = url
            break

    if body is None or used_url is None:
        log("FATAL: could not fetch valid shortage XML from any source. "
            "NOT publishing; yesterday's data remains live.")
        return 2

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_bytes(body)
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(used_url + "\n")
    log(f"wrote {OUT_FILE} ({len(body)} bytes); cached URL {used_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
