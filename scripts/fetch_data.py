"""Fetch the current medicine-shortage XML from Läkemedelsverket.

Runs on GitHub Actions runners (this data source is not reachable from
all environments; the pipeline is designed so every external fetch
happens here).

Strategy (fail closed at every step):
  1. Try the cached download URL (data/source_url.txt, committed).
  2. On failure, re-resolve the URL from Sveriges dataportal DCAT
     metadata -> Läkemedelsverket catalog distribution metadata.
  3. As a last resort, try the last-known hardcoded URL.
Every fetch has retries with exponential backoff and loud logging of
response codes. The downloaded XML is validated (namespace, root
element, minimum record count) before it is handed to the build step.
Exit codes: 0 = ok, 2 = fetch/validation failed (do not publish).
"""

from __future__ import annotations

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

RETRIES = 3
BACKOFF_SECONDS = [5, 15, 45]
TIMEOUT = 90


def log(msg: str) -> None:
    print(f"[fetch] {msg}", flush=True)


def http_get(url: str, accept: str = "*/*") -> bytes:
    """GET with retries + backoff. Raises on final failure."""
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": accept}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                status = getattr(resp, "status", 200)
                body = resp.read()
                log(f"GET {url} -> HTTP {status}, {len(body)} bytes (attempt {attempt})")
                return body
        except urllib.error.HTTPError as e:
            last_err = e
            log(f"GET {url} -> HTTP {e.code} {e.reason} (attempt {attempt})")
        except Exception as e:  # URLError, timeout, ...
            last_err = e
            log(f"GET {url} -> {type(e).__name__}: {e} (attempt {attempt})")
        if attempt < RETRIES:
            wait = BACKOFF_SECONDS[attempt - 1]
            log(f"retrying in {wait}s ...")
            time.sleep(wait)
    raise RuntimeError(f"all {RETRIES} attempts failed for {url}: {last_err}")


def resolve_download_url() -> str | None:
    """Resolve the XML download URL from dataportal DCAT metadata.

    dataset metadata (RDF/XML) references distribution resources at
    catalog.lakemedelsverket.se; each distribution's own metadata holds
    dcat:downloadURL. We pick the application/xml distribution whose
    URL contains 'current'.
    """
    try:
        rdf = http_get(DATAPORTAL_METADATA_URL, accept="application/rdf+xml").decode(
            "utf-8", "replace"
        )
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
            meta = http_get(meta_url, accept="application/rdf+xml").decode(
                "utf-8", "replace"
            )
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
            f"(minimum {min_records}) — refusing to publish"
        )
    return len(records)


def try_url(url: str) -> bytes | None:
    try:
        body = http_get(url, accept="application/xml")
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

    for url in [cached, None, LAST_KNOWN_DOWNLOAD_URL]:
        if url is None:
            # middle slot: runtime re-resolution via dataportal
            log("resolving download URL from Sveriges dataportal ...")
            url = resolve_download_url()
            if url is None:
                log("resolution produced no URL")
                continue
            log(f"resolved download URL: {url}")
        if url in tried:
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
