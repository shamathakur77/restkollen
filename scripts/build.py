"""Parse the fetched XML, diff against yesterday's snapshot, and write
site data + RSS feeds. Fail closed: any guard trip exits non-zero and
leaves the previously committed data untouched.

Inputs:
  work/current.xml            (from fetch_data.py; not committed)
  public/data/current.json    (yesterday's committed snapshot)
  data/events.json            (rolling change-event log, committed)

Outputs (only written when all guards pass):
  public/data/current.json    normalized product-level records
  public/data/diff.json       today's changes (new / back / date changed)
  public/data/meta.json       fetch timestamp, source, counts
  data/events.json            rolling event log (90 days)
  public/feeds/*.xml          RSS 2.0, one per category + all changes

Trust rules: only facts present in the source XML are emitted. Missing
or nullFlavor'd fields become null. Nothing is inferred.
"""

from __future__ import annotations

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from common import (
    CATEGORIES,
    EVENT_RETENTION_DAYS,
    FEED_MAX_ITEMS,
    FEED_WINDOW_DAYS,
    MAX_CHANGE_FRACTION,
    SITE_URL,
    STOCKHOLM,
    atc_in_category,
    now_stockholm_iso,
    record_key,
    today_stockholm,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
XML_IN = REPO_ROOT / "work" / "current.xml"
CURRENT_JSON = REPO_ROOT / "public" / "data" / "current.json"
DIFF_JSON = REPO_ROOT / "public" / "data" / "diff.json"
META_JSON = REPO_ROOT / "public" / "data" / "meta.json"
EVENTS_JSON = REPO_ROOT / "data" / "events.json"
FEEDS_DIR = REPO_ROOT / "public" / "feeds"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


# ---------------------------------------------------------------- parsing

def _text(el: ET.Element | None) -> str | None:
    """Element text, honouring nullFlavor attributes (masked/unknown/NA)."""
    if el is None:
        return None
    if el.get("nullFlavor"):
        return None
    t = (el.text or "").strip()
    return t or None


def parse_records(xml_bytes: bytes) -> tuple[list[dict], str]:
    """Parse source XML into product-level records.

    One record per (MedicineShortage, MedicinalProduct). Returns
    (records, creationDate).
    """
    root = ET.fromstring(xml_bytes)
    ns = root.tag[1 : root.tag.index("}")]

    def q(tag: str) -> str:
        return f"{{{ns}}}{tag}"

    creation = root.get("creationDate") or ""
    records: list[dict] = []

    for sh in root.iter(q("MedicineShortage")):
        shortage_id = _text(sh.find(q("MedicineShortageId")))
        if not shortage_id:
            continue
        type_of = _text(sh.find(q("TypeOfShortage")))
        first_pub = _text(sh.find(q("DateOfFirstPublication")))
        cause_cat = _text(sh.find(q("CauseOfShortageCategory")))
        cause = _text(sh.find(q("CauseOfShortage")))
        sh_updated = _text(sh.find(q("LastUpdated")))

        for mp in sh.iter(q("MedicinalProduct")):
            npl_id = _text(mp.find(q("NPLId")))
            if not npl_id:
                continue
            atc_el = mp.find(q("ATC"))
            packages = []
            for pk in mp.iter(q("PackagedMedicinalProduct")):
                iv = pk.find(q("Interval"))
                packages.append(
                    {
                        "packId": _text(pk.find(q("NPLPackId"))),
                        "description": _text(pk.find(q("PackageDescription"))),
                        "start": _text(iv.find(q("ForecastedStartDate"))) if iv is not None else None,
                        "expectedBack": _text(iv.find(q("ForecastedEndDate"))) if iv is not None else None,
                        "actualEnd": _text(iv.find(q("ActualEndDate"))) if iv is not None else None,
                    }
                )

            rec = {
                "shortageId": shortage_id,
                "nplId": npl_id,
                "product": _text(mp.find(q("ProductName"))),
                "substance": _text(mp.find(q("ActiveSubstances"))),
                "atc": _text(atc_el),
                "atcTerm": atc_el.get("term") if atc_el is not None else None,
                "mah": _text(mp.find(q("MarketAuthorisationHolderName"))),
                "isParallelImport": _text(mp.find(q("IsParallelImport"))),
                "typeOfShortage": type_of,
                "causeCategory": cause_cat,
                "cause": cause,
                "firstPublished": first_pub,
                "lastUpdated": sh_updated,
                "packages": packages,
            }
            rec.update(derive_status(rec))
            records.append(rec)

    return records, creation


def derive_status(rec: dict) -> dict:
    """Derive per-product status and expected-back date from package
    intervals. Pure aggregation of source facts, no inference:
      - a package is 'ended' when the source reports an ActualEndDate
      - 'active' when its start date has passed and no actual end
      - 'upcoming' when its start date is in the future
    Product expectedBack = latest ForecastedEndDate among not-ended
    packages (null if the source reports none).
    """
    today = today_stockholm()
    any_active = False
    any_upcoming = False
    all_ended = bool(rec["packages"])
    expected: list[str] = []
    starts: list[str] = []
    for p in rec["packages"]:
        if p["actualEnd"]:
            continue
        all_ended = False
        if p["start"] and p["start"] > today:
            any_upcoming = True
        else:
            any_active = True
        if p["expectedBack"]:
            expected.append(p["expectedBack"])
        if p["start"]:
            starts.append(p["start"])
    if any_active:
        status = "active"
    elif any_upcoming:
        status = "upcoming"
    elif all_ended:
        status = "ended"
    else:
        status = "active"  # no package info at all: report exists, treat as active
    return {
        "status": status,
        "expectedBack": max(expected) if expected else None,
        "start": min(starts) if starts else None,
    }


# ---------------------------------------------------------------- diffing

def summarize(rec: dict) -> dict:
    """Small projection of a record for diff/event/feed use."""
    return {
        "key": record_key(rec),
        "product": rec.get("product"),
        "substance": rec.get("substance"),
        "atc": rec.get("atc"),
        "expectedBack": rec.get("expectedBack"),
        "typeOfShortage": rec.get("typeOfShortage"),
    }


def compute_events(prev: list[dict], curr: list[dict], today: str) -> list[dict]:
    """Change events between yesterday's and today's records."""
    prev_by = {record_key(r): r for r in prev}
    curr_by = {record_key(r): r for r in curr}
    events: list[dict] = []

    for key, rec in curr_by.items():
        old = prev_by.get(key)
        if old is None:
            if rec["status"] != "ended":
                events.append({"type": "new", "date": today, **summarize(rec)})
            continue
        if old.get("status") != "ended" and rec["status"] == "ended":
            events.append({"type": "back", "date": today, **summarize(rec)})
            continue
        if rec["status"] != "ended" and old.get("expectedBack") != rec.get("expectedBack"):
            ev = {"type": "date_changed", "date": today, **summarize(rec)}
            ev["previousExpectedBack"] = old.get("expectedBack")
            events.append(ev)

    for key, old in prev_by.items():
        if key not in curr_by and old.get("status") != "ended":
            # disappeared from the current file: the shortage report is
            # no longer listed as current by Läkemedelsverket.
            events.append({"type": "back", "date": today, **summarize(old)})

    return events


# ---------------------------------------------------------------- feeds

LABELS = {
    "new": ("Ny restanmälan", "New shortage report"),
    "back": ("Restanmälan avslutad", "Shortage report ended"),
    "date_changed": ("Nytt förväntat datum", "Expected return date changed"),
}


def event_guid(ev: dict) -> str:
    raw = f"{ev['key']}:{ev['type']}:{ev['date']}:{ev.get('expectedBack')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def event_pubdate(ev: dict) -> str:
    d = date.fromisoformat(ev["date"])
    return format_datetime(datetime.combine(d, dtime(6, 0), tzinfo=STOCKHOLM))


def event_item_xml(ev: dict, link: str) -> str:
    sv, en = LABELS[ev["type"]]
    if ev.get("typeOfShortage") == "CESSATION" and ev["type"] == "new":
        sv, en = "Försäljning upphör", "Sales ending (cessation reported)"
    name = ev.get("product") or "uppgift saknas / not reported"
    back = ev.get("expectedBack")
    back_sv = back or "uppgift saknas"
    back_en = back or "not reported"
    title = f"{sv}: {name}"
    desc_parts = [
        f"{sv} / {en}.",
        f"Produkt / Product: {name}.",
    ]
    if ev.get("typeOfShortage") == "CESSATION":
        desc_parts.append(
            "Typ: anmält upphörande av försäljning / Type: reported sales cessation."
        )
    if ev.get("substance"):
        desc_parts.append(f"Substans / Substance: {ev['substance']}.")
    if ev.get("atc"):
        desc_parts.append(f"ATC: {ev['atc']}.")
    if ev["type"] == "date_changed" and ev.get("previousExpectedBack"):
        desc_parts.append(
            f"Tidigare förväntat åter / previously expected back: {ev['previousExpectedBack']}."
        )
    if ev["type"] != "back":
        desc_parts.append(
            f"Förväntas åter / Expected back: {back_sv} / {back_en}."
        )
    desc_parts.append(
        "Källa: Läkemedelsverkets öppna data. En restanmälan är inte samma sak "
        "som lagerstatus på apotek. / Source: Swedish MPA open data. A shortage "
        "report is not the same as pharmacy stock."
    )
    return (
        "    <item>\n"
        f"      <title>{escape(title)}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f"      <guid isPermaLink=\"false\">{event_guid(ev)}</guid>\n"
        f"      <pubDate>{event_pubdate(ev)}</pubDate>\n"
        f"      <description>{escape(' '.join(desc_parts))}</description>\n"
        "    </item>\n"
    )


def feed_xml(title: str, link: str, description: str, items: list[str], build_date: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(link)}</link>\n"
        f"    <description>{escape(description)}</description>\n"
        "    <language>sv</language>\n"
        f"    <lastBuildDate>{build_date}</lastBuildDate>\n"
        f"    <ttl>720</ttl>\n"
        + "".join(items)
        + "  </channel>\n</rss>\n"
    )


def write_feeds(all_events: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(STOCKHOLM).date() - timedelta(days=FEED_WINDOW_DAYS)).isoformat()
    recent = [e for e in all_events if e["date"] >= cutoff]
    recent.sort(key=lambda e: e["date"], reverse=True)
    build_date = format_datetime(datetime.now(STOCKHOLM))

    def render(events: list[dict], title: str, page: str, desc: str, path: Path) -> None:
        items = [
            event_item_xml(e, f"{SITE_URL}{page}")
            for e in events[:FEED_MAX_ITEMS]
        ]
        path.write_text(
            feed_xml(title, f"{SITE_URL}{page}", desc, items, build_date),
            encoding="utf-8",
        )

    render(
        recent,
        "Restkollen: alla ändringar / all changes",
        "/",
        "Nya, avslutade och uppdaterade restanmälningar för läkemedel i Sverige. "
        "Källa: Läkemedelsverkets öppna data. Inte medicinsk rådgivning.",
        out_dir / "alla.xml",
    )
    for cat in CATEGORIES:
        evs = [e for e in recent if atc_in_category(e.get("atc"), cat["prefixes"])]
        render(
            evs,
            f"Restkollen: {cat['sv']} / {cat['en']}",
            f"/kategori/{cat['slug']}",
            f"Ändringar i restanmälningar: {cat['sv']} (ATC {', '.join(cat['prefixes'])}). "
            "Källa: Läkemedelsverkets öppna data. Inte medicinsk rådgivning.",
            out_dir / f"{cat['slug']}.xml",
        )


# ---------------------------------------------------------------- main

def main() -> int:
    if not XML_IN.exists():
        log("FATAL: work/current.xml missing. run fetch_data.py first")
        return 2
    xml_bytes = XML_IN.read_bytes()

    try:
        records, creation = parse_records(xml_bytes)
    except Exception as e:
        log(f"FATAL: XML parse failed: {type(e).__name__}: {e}. NOT publishing")
        return 2
    log(f"parsed {len(records)} product-level records (source creationDate {creation})")

    prev_records: list[dict] = []
    first_run = True
    if CURRENT_JSON.exists():
        prev_payload = json.loads(CURRENT_JSON.read_text())
        prev_records = prev_payload.get("records", [])
        first_run = False

    today = today_stockholm()
    events = compute_events(prev_records, records, today) if not first_run else []

    # -------- fail-closed guard: implausibly large diff
    if not first_run and prev_records:
        fraction = len(events) / max(len(prev_records), 1)
        log(f"diff: {len(events)} events vs {len(prev_records)} previous records "
            f"({fraction:.1%} changed; limit {MAX_CHANGE_FRACTION:.0%})")
        if fraction > MAX_CHANGE_FRACTION:
            log("FATAL: diff exceeds plausibility limit. NOT publishing. "
                "Yesterday's data remains live. Inspect the source manually.")
            return 2
    elif first_run:
        log("first run: no previous snapshot, publishing without diff events")

    # -------- rolling event log (merge by guid: same-day re-runs are
    # additive and idempotent. a second run diffs against its own
    # snapshot and must not erase the day's earlier events)
    all_events: list[dict] = []
    if EVENTS_JSON.exists():
        all_events = json.loads(EVENTS_JSON.read_text()).get("events", [])
    seen_guids = {event_guid(e) for e in all_events}
    all_events.extend(e for e in events if event_guid(e) not in seen_guids)
    ret_cutoff = (datetime.now(STOCKHOLM).date() - timedelta(days=EVENT_RETENTION_DAYS)).isoformat()
    all_events = [e for e in all_events if e["date"] >= ret_cutoff]

    # -------- write outputs
    n_active = sum(1 for r in records if r["status"] == "active")
    n_upcoming = sum(1 for r in records if r["status"] == "upcoming")
    n_ended = sum(1 for r in records if r["status"] == "ended")

    CURRENT_JSON.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_JSON.write_text(
        json.dumps({"generated": now_stockholm_iso(), "sourceCreationDate": creation,
                    "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    todays_events = [e for e in all_events if e["date"] == today]
    DIFF_JSON.write_text(
        json.dumps(
            {
                "date": today,
                "firstRun": first_run,
                "new": [e for e in todays_events if e["type"] == "new"],
                "back": [e for e in todays_events if e["type"] == "back"],
                "dateChanged": [e for e in todays_events if e["type"] == "date_changed"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    META_JSON.write_text(
        json.dumps(
            {
                "lastVerified": now_stockholm_iso(),
                "sourceCreationDate": creation,
                "source": "Läkemedelsverket: Anmälda försäljningsuppehåll av läkemedel (öppna data, CC BY 4.0)",
                "counts": {"total": len(records), "active": n_active,
                           "upcoming": n_upcoming, "ended": n_ended},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    EVENTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    EVENTS_JSON.write_text(
        json.dumps({"events": all_events}, ensure_ascii=False), encoding="utf-8"
    )
    write_feeds(all_events, FEEDS_DIR)

    log(f"published: {len(records)} records ({n_active} active, {n_upcoming} upcoming, "
        f"{n_ended} recently ended); {len(events)} events today "
        f"({sum(1 for e in events if e['type']=='new')} new, "
        f"{sum(1 for e in events if e['type']=='back')} back, "
        f"{sum(1 for e in events if e['type']=='date_changed')} date changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
