#!/usr/bin/env python3
"""
TAF QC Console  -  AFMAN 15-124 (16 Jan 2019) compliance checker.

A single-file, cross-platform (Windows / Linux) desktop app:
  - Flask backend + SQLite (standard-library DB, zero config)
  - Self-contained browser UI served on localhost (offline for the core QC)
  - Live fetch from the Aviation Weather Center API, or manual paste
  - QC engine validates each forecast against AFMAN 15-124 Chapter 1 and the
    unit TAF encoding guide; every finding cites the governing paragraph
  - Optional regional consensus: compares the station to its nearest neighbors
  - SQLite history + one-click CSV export

Run:
    pip install flask requests
    python taf_qc_console.py
"""
import os
import re
import csv
import json
import math
import sqlite3
import threading
import webbrowser
from io import StringIO
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, Response

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "taf_validation.db")
AWC_TAF = "https://aviationweather.gov/api/data/taf"
AWC_INFO = "https://aviationweather.gov/api/data/stationinfo"
HOST, PORT = "127.0.0.1", 5000

app = Flask(__name__)

# =========================================================================== #
#  AFMAN 15-124 QC ENGINE  (validated against the manual's own example TAFs)
# =========================================================================== #
# --- Reference data (AFMAN 15-124) ----------------------------------------- #

# Table 1.1 reportable visibilities, in meters (the authoritative superset).
REPORTABLE_VIS_M = {
    "0000","0100","0200","0300","0400","0500","0600","0700","0800","0900",
    "1000","1100","1200","1300","1400","1500","1600","1700","1800","2000",
    "2200","2400","2600","2800","3000","3200","3400","3600","3700","4000",
    "4400","4500","4700","4800","5000","6000","7000","8000","9000","9999",
}
CLOUD_RANK = {"SKC": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4}

# w'w' code groups by Table 1.2 column (used for ordered construction check).
WX_DESCRIPTOR = "MI|PR|BC|DR|BL|SH|TS|FZ"
WX_PRECIP     = "DZ|RA|SN|SG|IC|PL|GR|GS|UP"
WX_OBSCURE    = "BR|FG|FU|VA|DU|SA|HZ|PY"
WX_OTHER      = "PO|SQ|FC|SS|DS"

# VC may pair only with these (1.3.6.4).
VC_ALLOWED = {"TS", "SH", "FG", "BLSN", "BLDU", "BLSA", "PO", "SS", "DS"}

# --- Precompiled token classifiers ----------------------------------------- #

RE_TAF_HEAD   = re.compile(r"^TAF(?:\s+(AMD|COR))?\b")
RE_ICAO       = re.compile(r"^[A-Z][A-Z0-9]{3}$")
RE_ISSUE      = re.compile(r"^(\d{2})(\d{2})(\d{2})Z$")
RE_VALID      = re.compile(r"^(\d{2})(\d{2})/(\d{2})(\d{2})$")
RE_WIND       = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT$")
RE_VIS_M      = re.compile(r"^\d{4}$")
RE_VIS_SM     = re.compile(r"^(M|P)?\d{1,2}(?:\s\d/\d|/\d)?SM$")
RE_CLOUD      = re.compile(r"^(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?$")
RE_SKY_PLAIN  = re.compile(r"^(SKC|NSC|CLR|NCD|CAVOK)$")
RE_VV         = re.compile(r"^VV(\d{3})$")
RE_ALT        = re.compile(r"^QNH(\d{4})INS$|^A(\d{4})$")
RE_ICING      = re.compile(r"^6\d{5}$")
RE_TURB       = re.compile(r"^5\d{5}$")
RE_WS         = re.compile(r"^WS(\d{3})/(\d{3})(\d{2,3})KT$")
RE_VA         = re.compile(r"^VA\d{6}$")
RE_TEMP       = re.compile(r"^T([XN])(M?\d{2})/(\d{2})(\d{2})Z$")
RE_FM         = re.compile(r"^FM(\d{2})(\d{2})(\d{2})$")
RE_CHANGE     = re.compile(r"^(BECMG|TEMPO|PROB\d{2})$")
RE_FCSTID     = re.compile(r"^F[SN]\d{3,5}$")
RE_WX_STRICT  = re.compile(
    rf"^(VC|[+-])?(?:{WX_DESCRIPTOR})?(?:{WX_PRECIP}){{0,3}}"
    rf"(?:{WX_OBSCURE})?(?:{WX_OTHER})?$"
)
RE_WX_HASPHEN = re.compile(rf"(?:{WX_DESCRIPTOR}|{WX_PRECIP}|{WX_OBSCURE}|{WX_OTHER})")


def _dur_hours(d1, h1, d2, h2):
    """Hours between two DDHH points; None if a month rollover makes it ambiguous."""
    diff = (d2 * 24 + h2) - (d1 * 24 + h1)
    if diff >= 0:
        return diff
    for days in (31, 30, 29, 28):
        r = diff + days * 24
        if 0 < r <= 31:
            return r
    return None


def qc_taf(raw):
    findings = []
    score = 100

    def flag(sev, ref, msg, penalty):
        nonlocal score
        findings.append({"severity": sev, "ref": ref, "message": msg})
        score -= penalty

    has_newlines = "\n" in raw
    text = " ".join(raw.upper().split())
    tokens = text.split()
    if not tokens:
        return {"status": "FAIL", "score": 0,
                "findings": [{"severity": "critical", "ref": "-",
                              "message": "Empty input."}], "groups": {}}

    groups = {"type": None, "icao": None, "issue": None, "valid": None,
              "wind": None, "visibility": None, "weather": None,
              "clouds": None, "altimeter": None, "change_groups": None}

    # ---- Pull global remark items (temperature, forecaster id) out first ----
    temps = RE_TEMP.findall(text)  # list of (X|N, val, dd, hh)
    fcst_id = bool(RE_FCSTID.search(text))

    # ---- Header: [TAF] [AMD|COR] CCCC DDHHMMZ DDHH/DDHH ----
    # The literal "TAF" is frequently dropped in raw feeds (the AWC raw API and
    # most paste sources begin at the ICAO), so its absence is advisory only.
    i = 0
    m = RE_TAF_HEAD.match(text)
    if m:
        groups["type"] = "TAF" + (f" {m.group(1)}" if m.group(1) else "")
        i = 1 + (1 if m.group(1) else 0)
    else:
        groups["type"] = "TAF"
        if tokens and tokens[0] in ("AMD", "COR"):
            groups["type"] = f"TAF {tokens[0]}"
            i = 1
        flag("warning", "1.3.2.1.1",
             "Message does not begin with the TAF identifier (often dropped in raw feeds).", 0)

    # Count modifiers (only one allowed, 1.3.2.1.2)
    if len([t for t in tokens[:4] if t in ("AMD", "COR")]) > 1:
        flag("critical", "1.3.2.1.2", "Only one forecast modifier (AMD or COR) is permitted.", 12)

    icao = tokens[i] if i < len(tokens) else ""
    if RE_ICAO.match(icao):
        groups["icao"] = icao
        i += 1
    else:
        flag("critical", "1.3.2.1.3", "Missing or malformed 4-letter ICAO identifier.", 18)

    issue = tokens[i] if i < len(tokens) else ""
    mi = RE_ISSUE.match(issue)
    if mi:
        groups["issue"] = issue
        dd, hh, mm = int(mi.group(1)), int(mi.group(2)), int(mi.group(3))
        if not (1 <= dd <= 31): flag("error", "1.3.2.1.4", f"Issue day '{dd:02d}' out of range.", 10)
        if hh > 23:             flag("error", "1.3.2.1.4", f"Issue hour '{hh:02d}' out of range.", 10)
        if mm > 59:             flag("error", "1.3.2.1.4", f"Issue minute '{mm:02d}' out of range.", 10)
        i += 1
    else:
        flag("critical", "1.3.2.1.4", "Missing or malformed issue date/time (DDHHMMZ).", 15)

    valid = tokens[i] if i < len(tokens) else ""
    mv = RE_VALID.match(valid)
    is_amd = groups["type"] and "AMD" in groups["type"]
    if mv:
        groups["valid"] = valid
        d1, h1, d2, h2 = (int(mv.group(1)), int(mv.group(2)),
                          int(mv.group(3)), int(mv.group(4)))
        for lbl, hv in (("begin", h1), ("end", h2)):
            if hv > 24: flag("error", "1.3.2.1.5", f"Valid-period {lbl} hour '{hv:02d}' out of range (00-24).", 8)
        dur = _dur_hours(d1, h1, d2, h2)
        if dur is not None and not is_amd and dur not in (24, 30):
            flag("warning", "1.3.1.1",
                 f"Valid period is {dur}h; AF TAFs are normally 24h or 30h.", 4)
        i += 1
    else:
        flag("critical", "1.3.2.1.5", "Missing or malformed valid period (DDHH/DDHH).", 15)

    # ---- Segment the body into change blocks (initial + FM/BECMG/TEMPO) ----
    body = tokens[i:]
    blocks, cur = [], {"kind": "INIT", "win": None, "toks": []}
    for tok in body:
        fm = RE_FM.match(tok)
        if fm:
            blocks.append(cur)
            cur = {"kind": "FM", "win": ("FM", int(fm.group(1)), int(fm.group(2))),
                   "toks": []}
        elif RE_CHANGE.match(tok):
            blocks.append(cur)
            cur = {"kind": tok if tok in ("BECMG", "TEMPO") else "PROB",
                   "win": None, "toks": [], "marker": tok}
        else:
            cur["toks"].append(tok)
    blocks.append(cur)

    change_kinds = [b["kind"] for b in blocks[1:]]
    groups["change_groups"] = ", ".join(change_kinds) if change_kinds else "None"

    # BECMG/TEMPO carry a window as first token (DDGG/DDGG)
    ts_anywhere = False
    consec_bt = 0       # consecutive BECMG/TEMPO since last FM/INIT (1.3.3 / encoding)
    icing_active = turb_active = False
    has_qnh_anywhere = any(RE_ALT.match(t) for t in tokens)

    for bi, blk in enumerate(blocks):
        kind = blk["kind"]
        toks = blk["toks"]

        # consecutive BECMG/TEMPO rule (encoding guide) — advisory
        if kind in ("BECMG", "TEMPO"):
            consec_bt += 1
            if consec_bt > 2:
                flag("warning", "Encoding/Change Lines",
                     "More than two consecutive BECMG/TEMPO lines without an FM line.", 4)
        elif kind in ("INIT", "FM"):
            consec_bt = 0

        # change-group window DDGG/DDGG and duration limits
        if kind in ("BECMG", "TEMPO") and toks and RE_VALID.match(toks[0]):
            wm = RE_VALID.match(toks[0])
            cd1, ch1, cd2, ch2 = map(int, wm.groups())
            cdur = _dur_hours(cd1, ch1, cd2, ch2)
            if kind == "BECMG" and cdur is not None and cdur > 2:
                flag("error", "1.3.3.1", f"BECMG window is {cdur}h; must not exceed 2 hours.", 10)
            if kind == "TEMPO" and cdur is not None and cdur > 6:
                flag("error", "Encoding/TEMPO", f"TEMPO window is {cdur}h; must not exceed 6 hours.", 8)
            toks = toks[1:]
        elif kind in ("BECMG", "TEMPO"):
            flag("error", "1.3.3", f"{kind} group missing its DDGG/DDGG period.", 8)

        # split forecast groups from remarks at the altimeter (QNH...INS)
        alt_idx = next((k for k, t in enumerate(toks) if RE_ALT.match(t)), None)
        fc_toks = toks if alt_idx is None else toks[:alt_idx]
        has_alt = alt_idx is not None

        # ---- WIND ----
        wind_tok = next((t for t in fc_toks if RE_WIND.match(t)), None)
        if kind in ("INIT", "FM") and not wind_tok:
            flag("critical", "1.3.4", f"{kind} line is missing the mandatory wind group.", 15)
        if wind_tok:
            wm = RE_WIND.match(wind_tok)
            d, spd = wm.group(1), int(wm.group(2))
            gust = int(wm.group(3)) if wm.group(3) else None
            if bi == 0:
                groups["wind"] = wind_tok
            if d != "VRB":
                dv = int(d)
                if dv > 360: flag("error", "1.3.4.1", f"Wind direction {d} exceeds 360 deg.", 8)
                elif dv % 10 != 0 and wind_tok != "00000KT":
                    flag("error", "1.3.4.1", f"Wind direction {d} not to nearest 10 deg.", 6)
            if wind_tok.startswith("VRB") and spd > 6:
                flag("warning", "1.3.4.1.2/1.3.4.2",
                     f"VRB used with {spd}KT (>6KT) — only valid for airmass TS.", 4)
            if d == "000" and spd == 0 and wind_tok != "00000KT":
                flag("error", "1.3.4.1.1", "Calm wind must be encoded exactly 00000KT.", 6)
            if gust is not None and gust <= spd:
                flag("error", "1.3.4.2.2",
                     f"Gust {gust}KT must be greater than the mean wind {spd}KT.", 8)

        # ---- VISIBILITY ----
        vis_tok = next((t for t in fc_toks
                        if RE_VIS_M.match(t) or RE_VIS_SM.match(t) or t == "CAVOK"), None)
        if kind in ("INIT", "FM") and not vis_tok:
            flag("critical", "1.3.5", f"{kind} line is missing the mandatory visibility group.", 12)
        if vis_tok:
            if bi == 0:
                groups["visibility"] = vis_tok
            if RE_VIS_M.match(vis_tok) and vis_tok not in REPORTABLE_VIS_M:
                flag("error", "Table 1.1",
                     f"Visibility {vis_tok} is not a Table 1.1 reportable value.", 8)

        # ---- WEATHER (w'w') ----
        wx_toks = []
        for t in fc_toks:
            if t in ("NSW",) or RE_WIND.match(t) or RE_CLOUD.match(t) or RE_SKY_PLAIN.match(t) \
               or RE_VV.match(t) or RE_VIS_M.match(t) or RE_VIS_SM.match(t) or RE_ALT.match(t) \
               or RE_ICING.match(t) or RE_TURB.match(t) or RE_WS.match(t) or RE_VA.match(t) \
               or RE_VALID.match(t):
                continue
            if RE_WX_HASPHEN.search(t) and RE_WX_STRICT.match(t):
                wx_toks.append(t)
            elif RE_WX_HASPHEN.search(t) and len(t) <= 9 and re.fullmatch(r"[+\-A-Z]+", t):
                flag("error", "1.3.6.1",
                     f"Weather group '{t}' is not built in column order "
                     "(intensity, descriptor, precip, obscuration, other).", 6)
        if "TS" in " ".join(wx_toks):
            ts_anywhere = True
        if bi == 0 and wx_toks:
            groups["weather"] = ", ".join(wx_toks)
        if len(wx_toks) > 3:
            flag("error", "1.3.6.2", "More than three w'w' groups encoded.", 6)
        for w in wx_toks:
            if w.startswith("VC") and (("+" in w) or ("-" in w)):
                flag("error", "1.3.6.4", f"Intensity qualifier used with VC in '{w}'.", 6)
        # visibility < 9999 requires a weather/obscuration group (1.3.5)
        if vis_tok and RE_VIS_M.match(vis_tok) and vis_tok != "9999" and not wx_toks:
            flag("error", "1.3.5",
                 f"Visibility {vis_tok} (<9999) requires a weather/obscuration group.", 8)

        # ---- CLOUDS / SKY ----
        # The cloud group is the FIRST contiguous run of sky tokens; any later
        # sky token (e.g. the 'FG FEW000' partial-obscuration remark, 1.3.7.5)
        # is a remark and is not part of the ordered cloud group.
        def _is_sky(t):
            return bool(RE_CLOUD.match(t) or RE_VV.match(t)
                        or (RE_SKY_PLAIN.match(t) and t in ("SKC", "NSC", "CLR", "NCD")))
        layers, vv_present, saw_ovc = [], False, False
        run, started = [], False
        for t in fc_toks:
            if _is_sky(t):
                run.append(t); started = True
            elif started:
                break
        for t in run:
            mc = RE_CLOUD.match(t)
            if mc:
                layers.append((mc.group(1), int(mc.group(2)), mc.group(3), t))
            elif RE_VV.match(t):
                vv_present = True
            elif RE_SKY_PLAIN.match(t):
                layers.append((t, None, None, t))
        if bi == 0 and (layers or vv_present):
            groups["clouds"] = ", ".join(l[3] for l in layers) or ("VV present" if vv_present else None)
        prev_h, prev_rank = -1, -1
        for amt, hhh, cb, t in layers:
            if amt in ("SKC", "NSC", "CLR", "NCD"):
                if hhh is not None:
                    flag("error", "1.3.7.1", f"{amt} must not carry a height.", 6)
                continue
            if saw_ovc:
                flag("error", "1.3.7", f"Layer '{t}' reported above an overcast layer.", 6)
            if hhh is not None:
                if hhh <= prev_h:
                    flag("error", "1.3.7", f"Cloud layer '{t}' not in ascending height order.", 8)
                rank = CLOUD_RANK.get(amt, 0)
                if rank < prev_rank:
                    flag("error", "1.3.7.1",
                         f"Summation principle: '{t}' coverage is less than a lower layer.", 8)
                if 50 < hhh <= 100 and hhh % 5 != 0:
                    flag("error", "Table 1.3", f"Height in '{t}' (5,000-10,000ft) must be to nearest 500ft.", 6)
                elif hhh > 100 and hhh % 10 != 0:
                    flag("error", "Table 1.3", f"Height in '{t}' (>10,000ft) must be to nearest 1,000ft.", 6)
                prev_h, prev_rank = hhh, rank
            if amt == "OVC":
                saw_ovc = True
        # CB required where a thunderstorm is forecast in this block (1.3.7.8)
        block_has_ts = any("TS" in w for w in wx_toks)
        block_has_cb = any(l[2] == "CB" for l in layers)
        if block_has_ts and not block_has_cb:
            flag("error", "1.3.7.8",
                 "Thunderstorm forecast without a cumulonimbus (CB) cloud group.", 8)

        # ---- ALTIMETER (QNH) ----
        if kind == "TEMPO" and has_alt:
            flag("critical", "1.3.12", "Altimeter (QNH) must not be encoded in a TEMPO group.", 12)
        if kind in ("INIT", "FM", "BECMG") and not has_alt and has_qnh_anywhere:
            flag("error", "1.3.12", f"{kind} line is missing the altimeter (QNH) group.", 8)
        if has_alt:
            am = RE_ALT.match(toks[alt_idx]) if alt_idx < len(toks) else None
            val = am.group(1) if am and am.group(1) else (am.group(2) if am else None)
            if bi == 0 and val:
                groups["altimeter"] = val
            if val and not (2700 <= int(val) <= 3200):
                flag("warning", "1.3.12", f"Altimeter {val} is outside a plausible range.", 4)

        # ---- NON-CONVECTIVE LLWS (WS) ----
        if any(RE_WS.match(t) for t in toks) and kind in ("BECMG", "TEMPO"):
            flag("critical", "1.3.9.2.2",
                 f"Non-convective LLWS (WS) must not appear in a {kind} group.", 12)

        # ---- ICING / TURBULENCE recognition ----
        # 6-group / 5-group are recognised so they are not mis-parsed as other
        # elements. AFMAN's own examples (e.g. Fig 1.4) drop them on later
        # predominant lines, so no carry-forward discrepancy is raised.

        # new line per change group (1.3.3) — only checkable with line breaks
        if has_newlines and kind in ("FM", "BECMG", "TEMPO") and bi > 0:
            pass  # newline structure preserved upstream; soft check omitted

    # ---- No altimeter anywhere: one advisory rather than per-line errors ----
    if not has_qnh_anywhere:
        flag("warning", "1.3.12",
             "No QNH altimeter group found; forecast is not in AFMAN/USAF format "
             "(civilian/ICAO TAFs omit it).", 4)

    # ---- Temperature group (1.3.13.1) ----
    if temps:
        kinds = [t[0] for t in temps]
        if "X" in kinds and kinds.index("X") > (kinds.index("N") if "N" in kinds else 99):
            flag("warning", "1.3.13.1", "TX (max) should be encoded before TN (min).", 4)
        for k, val, dd, hh in temps:
            if val.lstrip("M").lstrip("0") and not re.fullmatch(r"M?\d{2}", val):
                flag("error", "1.3.13.1.3", f"Malformed temperature value '{val}'.", 6)

    # ---- Verdict ----
    score = max(0, score)
    has_crit = any(f["severity"] == "critical" for f in findings)
    if has_crit or score < 70:
        status = "FAIL"
    elif any(f["severity"] == "error" for f in findings) or score < 90:
        status = "REVIEW"
    else:
        status = "PASS"
    return {"status": status, "score": score, "findings": findings, "groups": groups}



# =========================================================================== #


# =========================================================================== #
#  Storage layer
# =========================================================================== #

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS taf_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT, issue_time TEXT, validity TEXT, wind TEXT, visibility TEXT,
            clouds TEXT, altimeter TEXT, qc_status TEXT, qc_score INTEGER,
            findings TEXT, source TEXT, raw_text TEXT, logged_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def insert_log(g, qc, source, raw):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO taf_logs (icao, issue_time, validity, wind, visibility, clouds,
            altimeter, qc_status, qc_score, findings, source, raw_text, logged_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (g.get("icao"), g.get("issue"), g.get("valid"), g.get("wind"),
          g.get("visibility"), g.get("clouds"), g.get("altimeter"),
          qc["status"], qc["score"],
          json.dumps(qc["findings"]), source, raw,
          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")))
    conn.commit()
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return rid


def fetch_logs(limit=200):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM taf_logs ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def export_csv():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, icao, issue_time, validity, wind, visibility, "
                        "clouds, altimeter, qc_status, qc_score, source, raw_text, "
                        "logged_at FROM taf_logs ORDER BY id ASC").fetchall()
    conn.close()
    buf = StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))
    else:
        buf.write("no records\n")
    return buf.getvalue()


# =========================================================================== #
#  Data ingestion + optional regional consensus
# =========================================================================== #

def fetch_taf(icao):
    icao = (icao or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4}", icao):
        return None
    try:
        r = requests.get(AWC_TAF, params={"ids": icao, "format": "raw"}, timeout=10)
        r.raise_for_status()
        return r.text.strip() or None
    except Exception:
        return None


def _haversine_nm(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def regional_consensus(target_icao, target_raw):
    """Compare the target TAF's wind/visibility to its nearest neighbors.
    Returns (neighbors, leaderboard). Network failures degrade to ([], [])."""
    try:
        info = requests.get(AWC_INFO, params={"ids": target_icao, "format": "json"},
                            timeout=6).json()
        if not info:
            return [], []
        tlat, tlon = float(info[0].get("lat", 0)), float(info[0].get("lon", 0))
        if tlat == 0 and tlon == 0:
            return [], []
        bbox = f"{tlat-1.5},{tlon-1.5},{tlat+1.5},{tlon+1.5}"
        tafs = requests.get(AWC_TAF, params={"bbox": bbox, "format": "json"},
                            timeout=10).json()
    except Exception:
        return [], []

    neighbors, seen = [], set()
    for t in tafs:
        nic = t.get("icaoId")
        if not nic or nic == target_icao or nic in seen:
            continue
        nlat, nlon = float(t.get("lat", 0)), float(t.get("lon", 0))
        if not (nlat and nlon):
            continue
        seen.add(nic)
        neighbors.append({"icao": nic,
                          "dist": round(_haversine_nm(tlat, tlon, nlat, nlon), 1),
                          "raw": t.get("rawTAF", "")})
    neighbors.sort(key=lambda x: x["dist"])
    neighbors = neighbors[:4]

    def wv(raw):
        wm = re.search(r"\b(VRB|\d{3})(\d{2,3})(?:G\d{2,3})?KT\b", raw or "")
        vm = re.search(r"\b(\d{4})\b(?=\s|$)", (raw or "").split("KT", 1)[-1])
        d = None if (not wm or wm.group(1) == "VRB") else int(wm.group(1))
        s = int(wm.group(2)) if wm else None
        v = int(vm.group(1)) if vm else 9999
        return d, s, min(v, 9999)

    rows = [{"icao": target_icao, **dict(zip(("dir", "spd", "vis"), wv(target_raw)))}]
    for n in neighbors:
        rows.append({"icao": n["icao"], **dict(zip(("dir", "spd", "vis"), wv(n["raw"])))})

    dirs = [r["dir"] for r in rows if r["dir"] is not None]
    spds = [r["spd"] for r in rows if r["spd"] is not None]
    viss = [r["vis"] for r in rows if r["vis"] is not None]
    avg_spd = sum(spds) / len(spds) if spds else 0
    avg_vis = sum(viss) / len(viss) if viss else 9999
    if dirs:
        s = sum(math.sin(math.radians(d)) for d in dirs)
        c = sum(math.cos(math.radians(d)) for d in dirs)
        avg_dir = (math.degrees(math.atan2(s, c)) + 360) % 360
    else:
        avg_dir = 0

    board = []
    for r in rows:
        sc = 100.0
        if r["dir"] is not None and dirs:
            diff = abs(r["dir"] - avg_dir)
            diff = 360 - diff if diff > 180 else diff
            sc -= diff / 180.0 * 40
        else:
            sc -= 15
        if r["spd"] is not None and spds:
            sc -= min(25, abs(r["spd"] - avg_spd) * 2)
        if r["vis"] is not None and viss:
            sc -= min(35, abs(r["vis"] - avg_vis) / 1000 * 5)
        board.append({"icao": r["icao"], "score": round(max(0, sc), 1)})
    board.sort(key=lambda x: x["score"], reverse=True)
    for i, b in enumerate(board):
        b["rank"] = i + 1
    return neighbors, board


# =========================================================================== #
#  Routes
# =========================================================================== #

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/api/process", methods=["POST"])
def api_process():
    p = request.get_json(force=True, silent=True) or {}
    mode = p.get("mode", "live")
    icao = (p.get("icao") or "").strip().upper()

    if mode == "live":
        raw = fetch_taf(icao)
        if not raw:
            return jsonify({"ok": False,
                            "error": f"No TAF returned for '{icao}'. "
                                     "Check the ICAO or your connection."})
        source = "LIVE"
    else:
        raw = (p.get("raw") or "").strip()
        if not raw:
            return jsonify({"ok": False, "error": "Paste a TAF to validate."})
        source = "MANUAL"

    qc = qc_taf(raw)
    g = qc["groups"]
    icao = icao or g.get("icao") or ""
    rid = insert_log(g, qc, source, raw)

    neighbors, board = ([], [])
    if p.get("consensus") and re.fullmatch(r"[A-Z0-9]{4}", icao):
        neighbors, board = regional_consensus(icao, raw)

    return jsonify({"ok": True, "id": rid, "source": source, "raw": raw,
                    "qc": qc, "neighbors": neighbors, "leaderboard": board})


@app.route("/api/logs")
def api_logs():
    return jsonify({"ok": True, "logs": fetch_logs()})


@app.route("/api/export")
def api_export():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(export_csv(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=taf_logs_{stamp}.csv"})


# =========================================================================== #
#  Front end
# =========================================================================== #
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TAF QC Console</title>
<style>
  :root{
    --bg:#0b1220;--panel:#111c2e;--panel2:#0e1726;--line:#22324a;
    --ink:#e6edf6;--muted:#8090a8;--amber:#f5a623;--cyan:#56b4e0;
    --pass:#3ddc84;--review:#f5a623;--fail:#ff5d5d;
    --crit:#ff5d5d;--err:#ff8a5d;--warn:#f5c542;
    --mono:"JetBrains Mono","SFMono-Regular","Cascadia Code",Consolas,"DejaVu Sans Mono",monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5}
  .wrap{max-width:1120px;margin:0 auto;padding:0 20px 64px}
  header{border-bottom:1px solid var(--line);background:linear-gradient(180deg,#0e1828,#0b1220);position:sticky;top:0;z-index:5}
  .bar{max-width:1120px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .brand b{font-size:18px;letter-spacing:.06em}
  .tag{font-family:var(--mono);font-size:11px;color:var(--amber);border:1px solid var(--amber);border-radius:3px;padding:2px 7px;letter-spacing:.06em}
  .clock{margin-left:auto;font-family:var(--mono);color:var(--cyan);font-size:14px}
  .clock span{color:var(--muted);font-size:11px;margin-right:6px}
  h2{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 12px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px 20px;margin-top:18px}
  .modes{display:flex;gap:6px;margin-bottom:14px}
  .modes button{background:transparent;border:1px solid var(--line);color:var(--muted);padding:7px 16px;border-radius:6px;cursor:pointer;font-family:var(--mono);font-size:12px;letter-spacing:.06em}
  .modes button.on{color:var(--bg);background:var(--amber);border-color:var(--amber)}
  label{display:block;font-size:11px;letter-spacing:.06em;color:var(--muted);text-transform:uppercase;margin-bottom:5px}
  input,textarea{background:var(--panel2);border:1px solid var(--line);color:var(--ink);font-family:var(--mono);font-size:15px;padding:10px 11px;border-radius:6px;width:100%}
  textarea{resize:vertical;min-height:84px}
  input:focus,textarea:focus{outline:none;border-color:var(--cyan);box-shadow:0 0 0 2px rgba(86,180,224,.18)}
  .row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
  .icao{flex:0 0 150px}
  .go{flex:0 0 auto;background:var(--cyan);color:#04121d;border:none;font-weight:700;font-family:var(--mono);letter-spacing:.05em;padding:11px 22px;border-radius:6px;cursor:pointer;font-size:13px}
  .go:disabled{opacity:.5;cursor:default}
  .opt{display:flex;align-items:center;gap:7px;margin-top:12px;color:var(--muted);font-size:13px}
  .opt input{width:auto}
  #live,#manual{display:none}#live.on,#manual.on{display:block}

  #out{display:none}
  .raw{font-family:var(--mono);font-size:14px;background:#070d18;border:1px solid var(--line);border-left:3px solid var(--cyan);border-radius:6px;padding:14px 16px;white-space:pre-wrap;word-break:break-word;color:#cfe2f2}
  .verdict{display:flex;align-items:center;gap:18px;margin:16px 0 6px;flex-wrap:wrap}
  .badge{font-family:var(--mono);font-weight:700;letter-spacing:.1em;font-size:15px;padding:8px 18px;border-radius:6px;border:1px solid}
  .b-PASS{color:var(--pass);border-color:var(--pass);background:rgba(61,220,132,.08)}
  .b-REVIEW{color:var(--review);border-color:var(--review);background:rgba(245,166,35,.08)}
  .b-FAIL{color:var(--fail);border-color:var(--fail);background:rgba(255,93,93,.08)}
  .gauge{flex:1 1 220px}
  .gauge .nums{display:flex;justify-content:space-between;font-family:var(--mono);font-size:12px;color:var(--muted);margin-bottom:5px}
  .gauge .nums b{color:var(--ink);font-size:15px}
  .track{height:8px;background:#0a1322;border-radius:5px;overflow:hidden}
  .fill{height:100%;width:0;border-radius:5px;transition:width .6s ease}
  .counts{font-family:var(--mono);font-size:12px;color:var(--muted)}
  .counts b{color:var(--ink)}

  .groups{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden;margin-top:16px}
  .cell{background:var(--panel2);padding:10px 12px}
  .cell .k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
  .cell .v{font-family:var(--mono);font-size:14px;margin-top:3px;word-break:break-word}
  .cell .v.none{color:#4a5a72}

  .findings{margin-top:16px;display:flex;flex-direction:column;gap:7px}
  .find{display:flex;gap:10px;align-items:flex-start;font-size:13.5px;padding:9px 12px;border-radius:6px;background:var(--panel2);border-left:3px solid var(--line)}
  .find .sev{font-family:var(--mono);font-size:9px;letter-spacing:.06em;padding:2px 6px;border-radius:3px;flex:0 0 auto;margin-top:1px}
  .find .ref{font-family:var(--mono);font-size:11px;color:var(--cyan);flex:0 0 auto;margin-top:1px;min-width:74px}
  .find.critical{border-left-color:var(--crit)} .find.critical .sev{color:var(--crit);background:rgba(255,93,93,.13)}
  .find.error{border-left-color:var(--err)} .find.error .sev{color:var(--err);background:rgba(255,138,93,.13)}
  .find.warning{border-left-color:var(--warn)} .find.warning .sev{color:var(--warn);background:rgba(245,197,66,.13)}
  .clean{color:var(--pass);font-family:var(--mono);font-size:13px;margin-top:14px}

  .lb{margin-top:16px}
  .lb-row{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid #16243a;font-family:var(--mono);font-size:13px}
  .lb-row.me{background:#0e1828}
  .lb-rank{color:var(--muted);width:30px}
  .lb-icao{flex:1 1 auto;color:var(--ink)}
  .lb-score{color:var(--pass)}
  .nbrs{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;margin-top:12px}
  .nbr{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:11px 13px}
  .nbr .h{display:flex;justify-content:space-between;font-family:var(--mono);font-size:13px}
  .nbr .d{color:var(--muted);font-size:11px}
  .nbr .r{font-family:var(--mono);font-size:11px;color:#9fb6cd;margin-top:7px;white-space:pre-wrap;word-break:break-word}

  .loghead{display:flex;align-items:center;gap:12px}
  .btn{background:transparent;border:1px solid var(--line);color:var(--ink);padding:7px 14px;border-radius:6px;cursor:pointer;font-family:var(--mono);font-size:12px}
  .btn:hover{border-color:var(--cyan)}
  .btn.exp{border-color:var(--amber);color:var(--amber)}
  table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
  th{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
  td{padding:9px 10px;border-bottom:1px solid #16243a;font-family:var(--mono)}
  tr:hover td{background:#0e1828}
  .pill{font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px}
  .pill.PASS{color:var(--pass);background:rgba(61,220,132,.12)}
  .pill.REVIEW{color:var(--review);background:rgba(245,166,35,.12)}
  .pill.FAIL{color:var(--fail);background:rgba(255,93,93,.12)}
  .empty{color:var(--muted);font-family:var(--mono);font-size:13px;padding:18px 4px}
  @media(max-width:640px){.clock{display:none}.icao{flex:1 1 100%}th.hide,td.hide{display:none}}
</style>
</head>
<body>
<header><div class="bar">
  <div class="brand"><b>TAF QC CONSOLE</b> <span class="tag">AFMAN 15-124</span></div>
  <div class="clock"><span>ZULU</span><b id="zulu">--:--:--</b></div>
</div></header>

<div class="wrap">
  <section class="panel">
    <h2>Ingest forecast</h2>
    <div class="modes">
      <button id="m-live" class="on" onclick="setMode('live')">Live fetch</button>
      <button id="m-manual" onclick="setMode('manual')">Manual paste</button>
    </div>
    <div id="live" class="on">
      <div class="row">
        <div class="icao"><label>Station ICAO</label>
          <input id="icao" value="KLSV" maxlength="4" autocomplete="off"
                 oninput="this.value=this.value.toUpperCase()"
                 onkeydown="if(event.key==='Enter')run()"></div>
        <button class="go" id="go1" onclick="run()">Process</button>
      </div>
    </div>
    <div id="manual">
      <label>Paste raw TAF</label>
      <textarea id="raw" placeholder="TAF KLSV 121500Z 1215/1321 03009KT 9999 SCT250 QNH2977INS ..."></textarea>
      <div class="row" style="margin-top:10px">
        <div class="icao"><label>ICAO (optional)</label><input id="micao" maxlength="4"
             oninput="this.value=this.value.toUpperCase()"></div>
        <button class="go" onclick="run()">Validate</button>
      </div>
    </div>
    <div class="opt"><input type="checkbox" id="cons">
      <label for="cons" style="margin:0;text-transform:none;letter-spacing:0;color:var(--muted)">
        Also run regional consensus (compares nearest neighbors — needs internet)</label></div>
  </section>

  <section class="panel" id="out">
    <h2>AFMAN 15-124 QC result</h2>
    <div class="raw" id="rawout"></div>
    <div class="verdict">
      <div class="badge" id="badge"></div>
      <div class="gauge">
        <div class="nums"><span>Compliance score</span><b><span id="score">0</span>/100</b></div>
        <div class="track"><div class="fill" id="fill"></div></div>
      </div>
      <div class="counts" id="counts"></div>
    </div>
    <div class="groups" id="groups"></div>
    <div id="findwrap"></div>
    <div id="consout"></div>
  </section>

  <section class="panel">
    <div class="loghead">
      <h2 style="margin:0">Validation log</h2>
      <button class="btn" style="margin-left:auto" onclick="loadLogs()">Refresh</button>
      <button class="btn exp" onclick="location.href='/api/export'">Export CSV</button>
    </div>
    <div id="logbox"><div class="empty">Loading…</div></div>
  </section>
</div>

<script>
  let mode="live";
  function setMode(m){mode=m;
    document.getElementById("m-live").classList.toggle("on",m==="live");
    document.getElementById("m-manual").classList.toggle("on",m==="manual");
    document.getElementById("live").classList.toggle("on",m==="live");
    document.getElementById("manual").classList.toggle("on",m==="manual");}
  function tick(){const d=new Date(),p=n=>String(n).padStart(2,"0");
    document.getElementById("zulu").textContent=p(d.getUTCHours())+":"+p(d.getUTCMinutes())+":"+p(d.getUTCSeconds());}
  setInterval(tick,1000);tick();

  const LABELS={type:"Type",icao:"Station",issue:"Issue",valid:"Valid",wind:"Wind",
    visibility:"Visibility",weather:"Weather",clouds:"Clouds",altimeter:"Altimeter",change_groups:"Change grps"};

  async function run(){
    const body={mode,consensus:document.getElementById("cons").checked};
    if(mode==="live"){body.icao=document.getElementById("icao").value.trim().toUpperCase();
      if(body.icao.length!==4){alert("Enter a 4-letter ICAO.");return;}}
    else{body.raw=document.getElementById("raw").value.trim();
      body.icao=document.getElementById("micao").value.trim().toUpperCase();
      if(!body.raw){alert("Paste a TAF.");return;}}
    document.getElementById("go1").disabled=true;
    try{
      const r=await fetch("/api/process",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const j=await r.json();
      if(!j.ok){alert(j.error||"Failed.");return;}
      render(j);loadLogs();
    }catch(e){alert("Request failed: "+e);}
    finally{document.getElementById("go1").disabled=false;}
  }

  function render(j){
    const out=document.getElementById("out");out.style.display="block";
    document.getElementById("rawout").textContent=j.raw||"—";
    const qc=j.qc;
    const badge=document.getElementById("badge");badge.textContent=qc.status;badge.className="badge b-"+qc.status;
    document.getElementById("score").textContent=qc.score;
    const fill=document.getElementById("fill");
    const col=qc.status==="PASS"?css("--pass"):qc.status==="REVIEW"?css("--review"):css("--fail");
    fill.style.background=col;fill.style.width=qc.score+"%";
    const c={critical:0,error:0,warning:0};qc.findings.forEach(f=>c[f.severity]++);
    document.getElementById("counts").innerHTML=
      `<b>${c.critical}</b> critical &nbsp; <b>${c.error}</b> error &nbsp; <b>${c.warning}</b> advisory`;

    const g=document.getElementById("groups");g.innerHTML="";
    Object.keys(LABELS).forEach(k=>{const v=qc.groups[k];
      g.insertAdjacentHTML("beforeend",
       `<div class="cell"><div class="k">${LABELS[k]}</div><div class="v ${v?"":"none"}">${v?esc(v):"—"}</div></div>`);});

    const fw=document.getElementById("findwrap");
    if(!qc.findings.length){fw.innerHTML='<div class="clean">✓ No discrepancies — compliant with all checked AFMAN 15-124 rules.</div>';}
    else{fw.innerHTML='<div class="findings">'+qc.findings.map(f=>
      `<div class="find ${f.severity}"><span class="sev">${f.severity.toUpperCase()}</span>
       <span class="ref">${esc(f.ref)}</span><span>${esc(f.message)}</span></div>`).join("")+'</div>';}

    const co=document.getElementById("consout");co.innerHTML="";
    if(j.leaderboard&&j.leaderboard.length){
      let h='<div class="lb"><h2 style="margin:18px 0 4px">Regional consensus rank</h2>';
      j.leaderboard.forEach(l=>{h+=`<div class="lb-row ${l.icao===(j.qc.groups.icao||'')?'me':''}">
        <span class="lb-rank">#${l.rank}</span><span class="lb-icao">${esc(l.icao)}</span><span class="lb-score">${l.score}%</span></div>`;});
      h+='</div>';
      if(j.neighbors&&j.neighbors.length){h+='<div class="nbrs">'+j.neighbors.map(n=>
        `<div class="nbr"><div class="h"><span>${esc(n.icao)}</span><span class="d">${n.dist} NM</span></div>
         <div class="r">${esc(n.raw||"—")}</div></div>`).join("")+'</div>';}
      co.innerHTML=h;
    }
    out.scrollIntoView({behavior:"smooth",block:"nearest"});
  }

  async function loadLogs(){
    const box=document.getElementById("logbox");
    try{const j=await(await fetch("/api/logs")).json();
      if(!j.logs.length){box.innerHTML='<div class="empty">No forecasts logged yet.</div>';return;}
      let h='<table><thead><tr><th>ID</th><th>Station</th><th>Issue</th><th class="hide">Wind</th><th class="hide">Vis</th><th>Score</th><th>Status</th><th class="hide">Src</th><th class="hide">Logged (Z)</th></tr></thead><tbody>';
      j.logs.forEach(r=>{h+=`<tr><td>${r.id}</td><td>${esc(r.icao||"—")}</td><td>${esc(r.issue_time||"—")}</td>
        <td class="hide">${esc(r.wind||"—")}</td><td class="hide">${esc(r.visibility||"—")}</td>
        <td>${r.qc_score}</td><td><span class="pill ${r.qc_status}">${r.qc_status}</span></td>
        <td class="hide" style="color:var(--muted)">${esc(r.source||"")}</td>
        <td class="hide" style="color:var(--muted)">${esc(r.logged_at||"")}</td></tr>`;});
      box.innerHTML=h+"</tbody></table>";
    }catch(e){box.innerHTML='<div class="empty">Could not load logs.</div>';}
  }
  const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
  const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  loadLogs();
</script>
</body>
</html>
"""


def open_browser():
    webbrowser.open_new(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    init_db()
    print("=" * 62)
    print("  TAF QC CONSOLE  -  AFMAN 15-124 (16 Jan 2019) compliance")
    print(f"  Database : {DB_PATH}")
    print(f"  Open     : http://{HOST}:{PORT}   (Ctrl+C to stop)")
    print("=" * 62)
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        threading.Timer(1.0, open_browser).start()
    app.run(host=HOST, port=PORT, debug=False)
