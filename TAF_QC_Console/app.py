"""
app.py - TAF QC Console web service (Flask).

Deploy target: Render web service (gunicorn wsgi:app). Also runs locally:
    pip install -r requirements.txt
    python app.py

Storage: SQLite. Path configurable with TAF_DB_PATH (point it at a Render
persistent disk, e.g. /var/data/taf_validation.db; otherwise the DB is
ephemeral and resets on each deploy).
Exports: /api/export (CSV) and /api/export/xlsx (Excel, two sheets:
Validations + per-finding Findings rows).
"""
import os
import re
import csv
import json
import math
from io import StringIO, BytesIO
from datetime import datetime, timezone

import sqlite3
import requests
from flask import Flask, request, jsonify, Response, render_template, send_file
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from qc_engine import qc_taf

APP_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.environ.get("TAF_DB_PATH", os.path.join(APP_DIR, "taf_validation.db"))
AWC_TAF  = "https://aviationweather.gov/api/data/taf"
AWC_INFO = "https://aviationweather.gov/api/data/stationinfo"

app = Flask(__name__)


# =========================================================================== #
#  Storage layer (SQLite, parameterized queries only)
# =========================================================================== #

def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = _conn()
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


def upgrade_db():
    """Idempotent migrations: add normalized per-finding table + missing cols."""
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS taf_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id INTEGER NOT NULL REFERENCES taf_logs(id) ON DELETE CASCADE,
            severity TEXT NOT NULL,          -- critical | error | warning
            ref TEXT,                        -- AFMAN 15-124 paragraph / guide ref
            message TEXT
        )
    """)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(taf_logs)")}
    if "weather" not in cols:
        conn.execute("ALTER TABLE taf_logs ADD COLUMN weather TEXT")
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(taf_findings)")}
    if "penalty" not in fcols:
        conn.execute("ALTER TABLE taf_findings ADD COLUMN penalty INTEGER DEFAULT 0")
    if "token" not in fcols:
        conn.execute("ALTER TABLE taf_findings ADD COLUMN token TEXT")
    conn.commit()
    conn.close()


def insert_log(g, qc, source, raw):
    conn = _conn()
    cur = conn.execute("""
        INSERT INTO taf_logs (icao, issue_time, validity, wind, visibility, clouds,
            weather, altimeter, qc_status, qc_score, findings, source, raw_text, logged_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (g.get("icao"), g.get("issue"), g.get("valid"), g.get("wind"),
          g.get("visibility"), g.get("clouds"), g.get("weather"), g.get("altimeter"),
          qc["status"], qc["score"], json.dumps(qc["findings"]), source, raw,
          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")))
    rid = cur.lastrowid
    conn.executemany(
        "INSERT INTO taf_findings (log_id, severity, ref, message, penalty, token) "
        "VALUES (?,?,?,?,?,?)",
        [(rid, f["severity"], f["ref"], f["message"],
          f.get("penalty", 0), f.get("token")) for f in qc["findings"]])
    conn.commit()
    conn.close()
    return rid


def fetch_logs(limit=200):
    conn = _conn()
    rows = conn.execute("SELECT * FROM taf_logs ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


LOG_COLS = ("id", "icao", "issue_time", "validity", "wind", "visibility",
            "weather", "clouds", "altimeter", "qc_status", "qc_score",
            "source", "raw_text", "logged_at")
FIND_COLS = ("log_id", "icao", "qc_status", "severity", "ref", "penalty",
             "token", "message")


def _export_rows():
    conn = _conn()
    logs = conn.execute(
        f"SELECT {', '.join(LOG_COLS)} FROM taf_logs ORDER BY id ASC").fetchall()
    finds = conn.execute("""
        SELECT f.log_id, l.icao, l.qc_status, f.severity, f.ref,
               f.penalty, f.token, f.message
        FROM taf_findings f JOIN taf_logs l ON l.id = f.log_id
        ORDER BY f.log_id ASC, f.id ASC""").fetchall()
    conn.close()
    return logs, finds


def export_csv():
    logs, _ = _export_rows()
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(LOG_COLS)
    for r in logs:
        w.writerow([r[c] for c in LOG_COLS])
    return buf.getvalue()


def export_xlsx():
    logs, finds = _export_rows()
    wb = Workbook()

    head_fill = PatternFill("solid", fgColor="111C2E")
    head_font = Font(bold=True, color="FFFFFF")
    status_fill = {"PASS": "3DDC84", "REVIEW": "F5A623", "FAIL": "FF5D5D"}

    ws = wb.active
    ws.title = "Validations"
    ws.append([c.upper() for c in LOG_COLS])
    for cell in ws[1]:
        cell.fill, cell.font = head_fill, head_font
    st_col = LOG_COLS.index("qc_status") + 1
    for r in logs:
        ws.append([r[c] for c in LOG_COLS])
        fill = status_fill.get(r["qc_status"])
        if fill:
            ws.cell(row=ws.max_row, column=st_col).fill = PatternFill("solid", fgColor=fill)
    for col, width in zip("ABCDEFGHIJKLMN", (6, 8, 10, 11, 14, 11, 14, 18, 10, 10, 9, 40, 22)):
        ws.column_dimensions[col].width = width

    ws2 = wb.create_sheet("Findings")
    ws2.append([c.upper() for c in FIND_COLS])
    for cell in ws2[1]:
        cell.fill, cell.font = head_fill, head_font
    for r in finds:
        ws2.append([r[c] for c in FIND_COLS])
    for col, width in zip("ABCDEFGH", (8, 8, 10, 10, 18, 9, 14, 80)):
        ws2.column_dimensions[col].width = width

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


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
    return 3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))  # earth R in NM


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
        avg_dir = (math.degrees(math.atan2(s, c)) + 360) % 360  # vector-mean wind dir
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
#  Global JSON error handlers - the API must never return HTML error pages.
# =========================================================================== #

@app.errorhandler(Exception)
def _handle_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        # 404/405/400 etc. -> JSON with the same status code
        return jsonify({"ok": False, "error": e.description}), e.code
    app.logger.exception("Unhandled exception")
    # Never leak internals to the client; exception type only.
    return jsonify({"ok": False, "error": f"Internal error: {type(e).__name__}"}), 500


# =========================================================================== #
#  Routes
# =========================================================================== #

@app.route("/")
def index():
    return render_template("index.html")


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


MAX_UPLOAD_BYTES = 262_144   # 256 KB
MAX_UPLOAD_TAFS  = 50


def _split_tafs(blob):
    """Split an uploaded text blob into individual TAF messages.
    Blank-line separated blocks; a single block containing several 'TAF '
    headers is split before each header."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", blob) if b.strip()]
    if len(blocks) == 1 and blocks[0].upper().count("TAF ") > 1:
        parts = re.split(r"(?=\bTAF\s)", blocks[0], flags=re.I)
        blocks = [p.strip() for p in parts if p.strip()]
    return blocks[:MAX_UPLOAD_TAFS]


@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file supplied."}), 400
    blob = f.read(MAX_UPLOAD_BYTES + 1)
    if len(blob) > MAX_UPLOAD_BYTES:
        return jsonify({"ok": False, "error": "File exceeds 256 KB limit."}), 413
    try:
        textblob = blob.decode("utf-8", errors="replace")
    except Exception:
        return jsonify({"ok": False, "error": "File is not readable text."}), 400
    tafs = _split_tafs(textblob)
    if not tafs:
        return jsonify({"ok": False, "error": "No TAF text found in file."})
    results = []
    for raw in tafs:
        qc = qc_taf(raw)
        rid = insert_log(qc["groups"], qc, "UPLOAD", raw)
        results.append({"id": rid, "raw": raw, "qc": qc})
    return jsonify({"ok": True, "count": len(results), "results": results})


@app.route("/api/logs")
def api_logs():
    return jsonify({"ok": True, "logs": fetch_logs()})


@app.route("/api/export")
def api_export():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(export_csv(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=taf_logs_{stamp}.csv"})


@app.route("/api/export/xlsx")
def api_export_xlsx():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return send_file(export_xlsx(), as_attachment=True,
                     download_name=f"taf_logs_{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet")


if __name__ == "__main__":
    init_db()
    upgrade_db()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
