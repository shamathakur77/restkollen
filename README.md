# Restkollen

**Fri bevakning av restanmälda läkemedel i Sverige. / Free alerts for
Sweden's reported medicine shortages.**

Sweden has hundreds of medicines with open shortage reports
(*restanmälningar / försäljningsuppehåll*) at any given time. Patients
usually find out standing at the pharmacy counter. Restkollen publishes
a daily diff — new shortages, ended reports, changed return dates — in
Swedish and English, with free RSS feeds per category so anyone can be
alerted the day something changes. No accounts, no backend, no cost.

## What it publishes

- **Home page** — today's changes: new shortage reports, ended reports,
  updated expected-return dates.
- **Category pages** — live status grouped by ATC code: ADHD medicines
  (N06BA), hormone therapy MHT/HRT (G03), antibiotics (J01), diabetes
  (A10), contraception (G03A), painkillers (N02 + M01AE).
- **RSS feeds** — `/feeds/alla.xml` (all changes) plus one per category.
  One feed item per change event, with product name, strength, expected
  return date and link. Paste into any free RSS reader for alerts.
- **Search** — client-side, across all current shortage reports.

## Data source

[Läkemedelsverket](https://www.lakemedelsverket.se) (Swedish Medical
Products Agency) publishes reported shortages as open data, listed on
Sveriges dataportal as dataset
[140_2136 — Anmälda försäljningsuppehåll av läkemedel](https://www.dataportal.se/datasets/140_2136),
licensed **CC BY 4.0**, updated daily. The pipeline resolves the XML
distribution URL at runtime from the dataset's DCAT metadata
(`admin.dataportal.se` → `catalog.lakemedelsverket.se`), caches it in
`data/source_url.txt`, and re-resolves on failure.

## Architecture

```
GitHub Actions (cron 06:00 Europe/Stockholm + workflow_dispatch)
  │
  ├─ scripts/fetch_data.py
  │    resolve URL (cache → dataportal DCAT → last-known) 
  │    fetch with retries/backoff, loud logging
  │    validate: namespace, root element, ≥100 records   ── fail ⇒ exit 2
  │
  ├─ scripts/build.py
  │    parse XML → product-level records (facts only, nulls kept)
  │    diff vs yesterday's committed public/data/current.json
  │    guard: >40% records changed ⇒ exit 2, publish nothing
  │    write public/data/{current,diff,meta}.json
  │          data/events.json (rolling 90-day change log)
  │          public/feeds/*.xml (RSS 2.0)
  │
  ├─ git commit + push  (the repo is the database)
  │
  └─ Vercel deploy of static ./public  (plain HTML/CSS/JS, no backend,
     no auth, no cookies, no tracking, no LLM at runtime)
```

**Fail-closed:** if the fetch fails, the XML shape is wrong, or the diff
is implausibly large, nothing is published — yesterday's data stays
live, and every page shows a visible "data last verified {date}" stamp
(with a warning banner once it is ≥2 days old).

## Trust rules

- Only facts present in the source XML appear on the site. Missing
  fields render as *"uppgift saknas / not reported"*. Return dates,
  causes and substitutes are never inferred.
- No AI-generated medical content. Category descriptions are one
  static, human-written sentence each.
- Every page carries the standing disclaimer (SV + EN): **a registered
  shortage is not the same as pharmacy stock** — medicines can be on
  shelves while reported, or out of stock without being reported. Check
  [fass.se](https://www.fass.se) for pharmacy stock; talk to your
  pharmacist or doctor. **Restkollen is not medical advice.**

## Development

```bash
python -m pytest tests/ -q          # tests run on a bundled sample XML fixture
cp tests/fixtures/sample-current.xml work/current.xml
python scripts/build.py             # build data + feeds locally
cd public && python -m http.server  # preview
```

Note: the source endpoints are fetched by the GitHub Actions runner —
some sandboxed environments cannot reach them directly.

## License

Code: MIT. Data: © Läkemedelsverket, published under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); Restkollen
republishes it with attribution and a fetch timestamp.

*Not affiliated with Läkemedelsverket. Inte medicinsk rådgivning / not
medical advice.*
