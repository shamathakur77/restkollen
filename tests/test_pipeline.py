"""Tests for the Restkollen pipeline, run on the bundled sample fixture.

    cd restkollen && python -m pytest tests/ -q
"""

import copy
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build  # noqa: E402
import common  # noqa: E402
import fetch_data  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sample-current.xml"
FIXTURE_BYTES = FIXTURE.read_bytes()

# The fixture is written relative to this reference date.
TODAY = "2026-08-20"


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(build, "today_stockholm", lambda: TODAY)


def parse():
    return build.parse_records(FIXTURE_BYTES)


# ------------------------------------------------------------- validation

def test_validate_accepts_fixture():
    assert fetch_data.validate_xml(FIXTURE_BYTES, min_records=1) == 5


def test_validate_rejects_low_record_count():
    with pytest.raises(ValueError, match="refusing to publish"):
        fetch_data.validate_xml(FIXTURE_BYTES, min_records=100)


def test_validate_rejects_wrong_namespace():
    bad = FIXTURE_BYTES.replace(
        b"http://eservices.lakemedelsverket.se/opendata/medicineshortage/v3/",
        b"http://example.com/other-schema/",
    )
    with pytest.raises(ValueError, match="namespace"):
        fetch_data.validate_xml(bad, min_records=1)


def test_validate_rejects_malformed_xml():
    with pytest.raises(ET.ParseError):
        fetch_data.validate_xml(FIXTURE_BYTES[:500], min_records=1)


# ---------------------------------------------------------------- parsing

def test_parse_record_count_and_creation_date():
    records, creation = parse()
    # 5 shortages, one holds 2 products -> 6 product-level records
    assert len(records) == 6
    assert creation.startswith("2026-08-20T")


def test_parse_fields_verbatim_from_source():
    records, _ = parse()
    r = next(r for r in records if r["nplId"] == "20010101000001")
    assert r["product"] == "Metylfenidat Test 36 mg Depottablett"
    assert r["substance"] == "metylfenidathydroklorid"
    assert r["atc"] == "N06BA04"
    assert r["atcTerm"] == "Metylfenidat"
    assert r["mah"] == "Testbolag AB"
    assert r["causeCategory"] == "Produktionsproblem"
    assert len(r["packages"]) == 2


def test_nullflavor_becomes_null_never_guessed():
    records, _ = parse()
    r = next(r for r in records if r["nplId"] == "20020202000002")
    assert r["cause"] is None            # nullFlavor="MSK"
    assert r["causeCategory"] is None
    assert r["expectedBack"] is None     # ForecastedEndDate nullFlavor="UNK"
    assert r["status"] == "active"


def test_status_active_and_expected_back_aggregation():
    records, _ = parse()
    r = next(r for r in records if r["nplId"] == "20010101000001")
    # one package active (back 2026-10-15), one ended -> product active,
    # expectedBack from the still-active package only
    assert r["status"] == "active"
    assert r["expectedBack"] == "2026-10-15"


def test_status_upcoming():
    records, _ = parse()
    r = next(r for r in records if r["nplId"] == "20030303000003")
    assert r["status"] == "upcoming"  # starts 2026-09-15 > today


def test_status_ended():
    records, _ = parse()
    r = next(r for r in records if r["nplId"] == "20040404000004")
    assert r["status"] == "ended"     # only package has ActualEndDate


# ---------------------------------------------------------------- diffing

def test_first_diff_events():
    records, _ = parse()
    prev = copy.deepcopy(records)

    # simulate yesterday: semaglutid was still active (no actual end)
    sem_prev = next(r for r in prev if r["nplId"] == "20040404000004")
    sem_prev["status"] = "active"
    # naproxen previously expected back earlier
    nap_prev = next(r for r in prev if r["nplId"] == "20050505000005")
    nap_prev["expectedBack"] = "2026-09-01"
    # estradiol record did not exist yesterday
    prev = [r for r in prev if r["nplId"] != "20020202000002"]
    # a record that disappeared from today's file entirely
    ghost = copy.deepcopy(nap_prev)
    ghost["shortageId"] = "gone-0000"
    ghost["nplId"] = "20099999000009"
    ghost["product"] = "Försvunnet Test 10 mg Tablett"
    prev.append(ghost)

    events = build.compute_events(prev, records, TODAY)
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)

    assert [e["key"] for e in by_type["new"]] == [
        "aaaa1111-0000-0000-0000-000000000002:20020202000002"
    ]
    back_keys = {e["key"] for e in by_type["back"]}
    assert "aaaa1111-0000-0000-0000-000000000004:20040404000004" in back_keys
    assert "gone-0000:20099999000009" in back_keys
    changed = by_type["date_changed"]
    assert len(changed) == 1
    assert changed[0]["previousExpectedBack"] == "2026-09-01"
    assert changed[0]["expectedBack"] == "2026-12-01"


def test_new_but_already_ended_record_is_not_an_alert():
    records, _ = parse()
    ended_only = [r for r in records if r["nplId"] == "20040404000004"]
    events = build.compute_events([], ended_only, TODAY)
    assert events == []


# ------------------------------------------------------- fail-closed guard

def run_main(tmp_path, monkeypatch, xml_bytes, prev_payload=None):
    monkeypatch.setattr(build, "XML_IN", tmp_path / "work" / "current.xml")
    monkeypatch.setattr(build, "CURRENT_JSON", tmp_path / "public" / "data" / "current.json")
    monkeypatch.setattr(build, "DIFF_JSON", tmp_path / "public" / "data" / "diff.json")
    monkeypatch.setattr(build, "META_JSON", tmp_path / "public" / "data" / "meta.json")
    monkeypatch.setattr(build, "EVENTS_JSON", tmp_path / "data" / "events.json")
    monkeypatch.setattr(build, "FEEDS_DIR", tmp_path / "public" / "feeds")
    build.XML_IN.parent.mkdir(parents=True, exist_ok=True)
    build.XML_IN.write_bytes(xml_bytes)
    if prev_payload is not None:
        build.CURRENT_JSON.parent.mkdir(parents=True, exist_ok=True)
        build.CURRENT_JSON.write_text(json.dumps(prev_payload, ensure_ascii=False))
    return build.main()


def test_main_first_run_publishes(tmp_path, monkeypatch):
    assert run_main(tmp_path, monkeypatch, FIXTURE_BYTES) == 0
    data = json.loads((tmp_path / "public" / "data" / "current.json").read_text())
    assert len(data["records"]) == 6
    diff = json.loads((tmp_path / "public" / "data" / "diff.json").read_text())
    assert diff["firstRun"] is True
    meta = json.loads((tmp_path / "public" / "data" / "meta.json").read_text())
    assert meta["counts"]["total"] == 6


def test_main_fails_closed_on_implausible_diff(tmp_path, monkeypatch):
    records, _ = parse()
    # yesterday held entirely different records -> ~200% change
    prev = []
    for i in range(6):
        r = copy.deepcopy(records[0])
        r["shortageId"] = f"old-{i}"
        r["nplId"] = f"3000000000000{i}"
        prev.append(r)
    prev_payload = {"generated": "x", "records": prev}
    assert run_main(tmp_path, monkeypatch, FIXTURE_BYTES, prev_payload) == 2
    # published data untouched (still yesterday's snapshot)
    data = json.loads((tmp_path / "public" / "data" / "current.json").read_text())
    assert data == prev_payload
    assert not (tmp_path / "public" / "data" / "diff.json").exists()


def test_main_normal_small_diff_publishes(tmp_path, monkeypatch):
    records, _ = parse()
    prev = copy.deepcopy(records)
    prev = [r for r in prev if r["nplId"] != "20020202000002"]  # one new today
    prev_payload = {"generated": "x", "records": prev}
    assert run_main(tmp_path, monkeypatch, FIXTURE_BYTES, prev_payload) == 0
    diff = json.loads((tmp_path / "public" / "data" / "diff.json").read_text())
    assert len(diff["new"]) == 1
    assert diff["new"][0]["product"].startswith("Estradiol Test")


def test_malformed_xml_fails_closed(tmp_path, monkeypatch):
    assert run_main(tmp_path, monkeypatch, b"<broken") == 2
    assert not (tmp_path / "public" / "data" / "current.json").exists()


# ------------------------------------------------------------------ feeds

def test_feeds_are_wellformed_rss2_and_categorized(tmp_path, monkeypatch):
    records, _ = parse()
    prev = copy.deepcopy(records)
    prev = [r for r in prev if r["nplId"] != "20020202000002"]
    sem = next(r for r in prev if r["nplId"] == "20040404000004")
    sem["status"] = "active"
    assert run_main(tmp_path, monkeypatch, FIXTURE_BYTES,
                    {"generated": "x", "records": prev}) == 0

    feeds_dir = tmp_path / "public" / "feeds"
    expected = {"alla.xml"} | {c["slug"] + ".xml" for c in common.CATEGORIES}
    assert {p.name for p in feeds_dir.iterdir()} == expected

    for path in feeds_dir.iterdir():
        root = ET.parse(path).getroot()          # well-formed
        assert root.tag == "rss" and root.get("version") == "2.0"
        channel = root.find("channel")
        assert channel.find("title") is not None
        assert channel.find("link") is not None
        assert channel.find("description") is not None
        for item in channel.findall("item"):
            assert item.find("title") is not None
            assert item.find("guid") is not None
            assert item.find("pubDate") is not None

    # the estradiol "new" event must be in hormonbehandling (G03) but the
    # semaglutid "back" event only in diabetes (A10)
    horm = (feeds_dir / "hormonbehandling.xml").read_text()
    assert "Estradiol Test" in horm and "Semaglutid" not in horm
    dia = (feeds_dir / "diabetes.xml").read_text()
    assert "Semaglutid" in dia and "Restanmälan avslutad" in dia
    alla = (feeds_dir / "alla.xml").read_text()
    assert "Estradiol Test" in alla and "Semaglutid" in alla


def test_feed_guids_stable_and_unique(tmp_path, monkeypatch):
    ev1 = {"key": "a:1", "type": "new", "date": TODAY, "expectedBack": "2026-10-01"}
    ev2 = {"key": "a:1", "type": "date_changed", "date": TODAY, "expectedBack": "2026-11-01"}
    assert build.event_guid(ev1) == build.event_guid(dict(ev1))
    assert build.event_guid(ev1) != build.event_guid(ev2)


# ------------------------------------------------------------- categories

def test_category_matching():
    assert common.atc_in_category("N06BA04", ["N06BA"])
    assert common.atc_in_category("G03AC08", ["G03"])      # contraception also in G03
    assert common.atc_in_category("M01AE02", ["N02", "M01AE"])
    assert not common.atc_in_category("A10BJ06", ["N02", "M01AE"])
    assert not common.atc_in_category(None, ["N02"])


def test_same_day_rerun_keeps_events(tmp_path, monkeypatch):
    """cron after a manual run must not erase the day's events/feeds."""
    records, _ = parse()
    prev = [r for r in copy.deepcopy(records) if r["nplId"] != "20020202000002"]
    assert run_main(tmp_path, monkeypatch, FIXTURE_BYTES,
                    {"generated": "x", "records": prev}) == 0
    diff1 = json.loads((tmp_path / "public" / "data" / "diff.json").read_text())
    assert len(diff1["new"]) == 1
    # second run same day: previous snapshot is now today's own output
    assert build.main() == 0
    diff2 = json.loads((tmp_path / "public" / "data" / "diff.json").read_text())
    assert len(diff2["new"]) == 1  # event survives the re-run
    horm = (tmp_path / "public" / "feeds" / "hormonbehandling.xml").read_text()
    assert "Estradiol Test" in horm
