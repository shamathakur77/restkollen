"""Shared configuration and helpers for the Restkollen pipeline.

Source of truth: Läkemedelsverket's open data on medicine shortages
(dataset 140_2136 on Sveriges dataportal, "Anmälda försäljningsuppehåll
av läkemedel", CC BY 4.0). Only facts present in the source XML are
published. Nothing is inferred.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

STOCKHOLM = ZoneInfo("Europe/Stockholm")

# XML namespace of the v3 open data schema.
NS_PREFIX = "http://eservices.lakemedelsverket.se/opendata/medicineshortage/"
NS_V3 = "http://eservices.lakemedelsverket.se/opendata/medicineshortage/v3/"

# Dataset metadata on Sveriges dataportal (DCAT, RDF/XML).
DATAPORTAL_METADATA_URL = "https://admin.dataportal.se/store/140/metadata/2136"

# Last known direct download URL (resolved 2026-08-20 from the DCAT
# distribution metadata at catalog.lakemedelsverket.se). Used as a
# fallback if runtime resolution fails; runtime resolution wins.
LAST_KNOWN_DOWNLOAD_URL = (
    "https://docetp.mpa.se/LMF/Reports/opendata-medicine-shortages-current-3-0.xml"
)

# Fail-closed guards.
MIN_RECORDS = 100          # current file typically holds several hundred records
MAX_CHANGE_FRACTION = 0.40  # abort publish if more than 40% of records changed

# Rolling event log / feed retention.
EVENT_RETENTION_DAYS = 90
FEED_WINDOW_DAYS = 60
FEED_MAX_ITEMS = 100

SITE_URL = os.environ.get("RESTKOLLEN_SITE_URL", "https://restkollen.vercel.app")

USER_AGENT = "Restkollen/1.0 (open-source shortage alerts; github.com/shamathakur77/restkollen)"

# Categories, keyed by ATC-code prefixes. Descriptions are static,
# human-written, factual one-liners — never AI medical content.
CATEGORIES = [
    {
        "slug": "adhd",
        "sv": "ADHD-läkemedel",
        "en": "ADHD medicines",
        "prefixes": ["N06BA"],
    },
    {
        "slug": "hormonbehandling",
        "sv": "Hormonbehandling (MHT/HRT)",
        "en": "Hormone therapy (MHT/HRT)",
        "prefixes": ["G03"],
    },
    {
        "slug": "antibiotika",
        "sv": "Antibiotika",
        "en": "Antibiotics",
        "prefixes": ["J01"],
    },
    {
        "slug": "diabetes",
        "sv": "Diabetesläkemedel",
        "en": "Diabetes medicines",
        "prefixes": ["A10"],
    },
    {
        "slug": "preventivmedel",
        "sv": "Preventivmedel",
        "en": "Contraception",
        "prefixes": ["G03A"],
    },
    {
        "slug": "smartstillande",
        "sv": "Smärtstillande",
        "en": "Painkillers",
        "prefixes": ["N02", "M01AE"],
    },
]


def today_stockholm() -> str:
    """Today's date (YYYY-MM-DD) in Europe/Stockholm."""
    return datetime.now(STOCKHOLM).date().isoformat()


def now_stockholm_iso() -> str:
    return datetime.now(STOCKHOLM).isoformat(timespec="seconds")


def atc_in_category(atc: str | None, prefixes: list[str]) -> bool:
    if not atc:
        return False
    return any(atc.startswith(p) for p in prefixes)


def record_key(rec: dict) -> str:
    """Stable key for one product within one shortage report."""
    return f"{rec['shortageId']}:{rec['nplId']}"
