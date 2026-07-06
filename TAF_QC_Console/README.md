# TAF QC Console

AFMAN 15-124 (16 Jan 2019) compliance checker for Terminal Aerodrome Forecasts.

Try it out here: https://taf-qc-console.onrender.com

## Features
- **QC engine** (`qc_engine.py`): validates each TAF against AFMAN 15-124
  Chapter 1 and the unit TAF encoding guide. Every finding cites the governing
  paragraph (e.g. `1.3.4.2.2`, `Table 1.1`). Score starts at 100; severity-
  weighted penalties yield PASS (>=90, no errors), REVIEW, or FAIL
  (any critical finding or score < 70).
- **Live fetch** from the Aviation Weather Center API, manual paste, or
  file upload (`/api/upload`, plain text, one or more TAFs, 256 KB cap).
- **Inline highlighting**: each finding carries a character span into the
  normalized TAF; the UI marks the exact token where points were lost
  ("TAF: 65 · -6 · 1.3.4.1 · Wind direction 255 not to nearest 10 deg")
  with severity-tinted highlights and hover tooltips.
- **Reference popup**: message template + checked-rule summary with
  paragraph citations, one click from the nav.
- **Regional consensus** (optional): ranks the station against its nearest
  neighbors on wind/visibility agreement.
- **SQL storage**: SQLite. `taf_logs` (one row per validation) +
  `taf_findings` (one row per discrepancy, FK to `taf_logs`).
- **Exports**: `/api/export` (CSV) and `/api/export/xlsx` (Excel workbook,
  Validations + Findings sheets).

## UI
Minimal white design: pill buttons (soft white for Export CSV / Export
Excel / Upload, blue for Process), segmented Live / Paste / Upload input,
findings list linked to in-text highlights.

## Run locally
```
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

## Deploy (Render)
- Build: `pip install -r requirements.txt`
- Start: `gunicorn wsgi:app`
- Set `TAF_DB_PATH` to a path on a persistent disk (e.g.
  `/var/data/taf_validation.db`). Without a persistent disk the SQLite
  history resets on every deploy/restart.

## Checked rules (summary)
Header (TAF/AMD/COR, ICAO, issue time, valid period 24/30h), wind
(direction to 10 deg, calm = 00000KT, VRB only <=6KT, gust spread >=10KT),
visibility (Table 1.1 reportable values, w'w' required < 9999m), weather
(Table 1.2 column order, max three groups, VC pairing), sky cover
(ascending bases, summation principle, Table 1.3 height increments, CB
required with TS), altimeter (QNH....INS, prohibited in TEMPO), LLWS
(prohibited in BECMG/TEMPO), change groups (BECMG <= 2h, TEMPO <= 6h,
consecutive-line limits), and TX/TN temperature groups.
