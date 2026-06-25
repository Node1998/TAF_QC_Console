import os
import re
import csv
import sqlite3
from io import StringIO
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, Response

APP_DIR = os.getcwd()
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "taf_validation.db"))
AWC_URL = "https://aviationweather.gov/api/data/taf"

app = Flask(__name__)
_WX = r"VC|MI|BC|PR|DR|BL|SH|TS|FZ|DZ|RA|SN|SG|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS"

# ==========================================
# 🗄️ STORAGE LAYER (DATABASE)
# ==========================================

def init_db():
    """Initializes the database schema if it doesn't exist."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS taf_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT, report_type TEXT, issue_time TEXT, validity TEXT,
            wind TEXT, wind_speed INTEGER, wind_gust INTEGER, visibility TEXT,
            cloud_layers TEXT, weather_phenomena TEXT, altimeter TEXT,
            change_groups TEXT, qc_status TEXT, qc_score INTEGER,
            source TEXT, raw_text TEXT, logged_at TEXT,
            regional_avg INTEGER, regional_flag TEXT
        )
    """)
    conn.commit()
    conn.close()

def upgrade_db():
    """Safely upgrades database schema by adding regional comparison columns."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute("PRAGMA table_info(taf_logs)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'regional_avg' not in columns:
            conn.execute("ALTER TABLE taf_logs ADD COLUMN regional_avg INTEGER")
        if 'regional_flag' not in columns:
            conn.execute("ALTER TABLE taf_logs ADD COLUMN regional_flag TEXT")
            conn.commit()
    except sqlite3.OperationalError as e:
        print(f"Database migration note: {e}")
    finally:
        conn.close()

def insert_log(data, qc, source, regional_avg=None, regional_flag=None):
    """Inserts a completed check with regional flags into SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO taf_logs (
            icao, report_type, issue_time, validity, wind, wind_speed,
            wind_gust, visibility, cloud_layers, weather_phenomena, altimeter,
            change_groups, qc_status, qc_score, source, raw_text, logged_at,
            regional_avg, regional_flag
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data.get("icao"), data.get("type"), data.get("issue_time"),
        data.get("validity"), data.get("wind"), data.get("wind_speed"),
        data.get("wind_gust"), data.get("visibility"), data.get("cloud_layers"),
        data.get("weather_phenomena"), data.get("altimeter"), data.get("change_groups"),
        qc["status"], qc["score"], source, data.get("raw_text"),
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        regional_avg, regional_flag
    ))
    conn.commit()
    rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return rowid

def fetch_logs(limit=200):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM taf_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def export_csv():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM taf_logs ORDER BY id ASC").fetchall()
    conn.close()
    buf = StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
    else:
        buf.write("no records\n")
    return buf.getvalue()

# ==========================================
# ✈️ METAR/TAF DATA PROCESSING & REGIONAL ANALYSIS
# ==========================================

def fetch_taf(icao):
    icao = (icao or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4}", icao):
        return None
    try:
        resp = requests.get(AWC_URL, params={"ids": icao, "format": "raw"}, timeout=10)
        resp.raise_for_status()
        return resp.text.strip() or None
    except Exception:
        return None

def parse_taf(clean):
    parsed = {"type": "TAF", "raw_text": clean}
    icao_m = re.search(r"\b([A-Z0-9]{4})\b", clean)
    parsed["icao"] = icao_m.group(1) if icao_m else None
    
    time_m = re.search(r"\b(\d{6}Z)\b", clean)
    parsed["issue_time"] = time_m.group(1) if time_m else None
    
    valid_m = re.search(r"\b(\d{4}/\d{4})\b", clean)
    parsed["validity"] = valid_m.group(1) if valid_m else None

    wind_m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b", clean)
    if wind_m:
        parsed["wind"] = wind_m.group(0)
        parsed["wind_dir"] = wind_m.group(1)
        parsed["wind_speed"] = int(wind_m.group(2))
        parsed["wind_gust"] = int(wind_m.group(3)) if wind_m.group(3) else 0
    else:
        parsed["wind"], parsed["wind_dir"], parsed["wind_speed"], parsed["wind_gust"] = None, None, None, 0

    region = clean[wind_m.end():] if wind_m else clean
    vis_m = re.search(r"\b(CAVOK|P6SM|M?\d+\s+\d+/\d+SM|M?\d+/\d+SM|M?\d{1,2}SM|\d{4})\b", region)
    parsed["visibility"] = vis_m.group(1) if vis_m else None

    alt_m = re.search(r"\bQNH(\d{4})INS\b", clean) or re.search(r"\bA(\d{4})\b", clean)
    parsed["altimeter"] = alt_m.group(1) if alt_m else None

    clouds = re.findall(r"\b((?:FEW|SCT|BKN|OVC|VV)\d{3}(?:CB|TCU)?|SKC|CLR|NSC|NCD)\b", clean)
    parsed["cloud_layers"] = ", ".join(clouds) if clouds else None

    wx = re.findall(r"\b(" + _WX + r")\b", clean)
    parsed["weather_phenomena"] = ", ".join(wx) if wx else None

    cg = re.findall(r"\b(BECMG|TEMPO|FM\d{6}|PROB\d{2})", clean)
    parsed["change_groups"] = ", ".join(cg) if cg else None
    return parsed

def qc_process(d):
    findings = []
    score = 100
    def flag(severity, message, penalty):
        nonlocal score
        findings.append({"severity": severity, "message": message})
        score -= penalty

    mandatory = {"type": "Report type (TAF)", "icao": "Station identifier", "issue_time": "Issue time", "validity": "Valid period", "wind": "Wind group", "visibility": "Visibility"}
    for key, label in mandatory.items():
        if not d.get(key):
            flag("critical", f"Missing mandatory group: {label}", 20)

    if d.get("icao") and not re.fullmatch(r"[A-Z][A-Z0-9]{3}", d["icao"]):
        flag("error", f"Station '{d['icao']}' is not a valid 4-character ICAO", 15)

    it = d.get("issue_time")
    if it:
        if not re.fullmatch(r"\d{6}Z", it):
            flag("error", "Issue time must be DDHHMMZ (e.g. 121500Z)", 15)
        else:
            dd, hh, mm = int(it[0:2]), int(it[2:4]), int(it[4:6])
            if not 1 <= dd <= 31: flag("error", f"Issue day '{dd:02d}' is out of range", 10)
            if hh > 23: flag("error", f"Issue hour '{hh:02d}' is out of range", 10)
            if mm > 59: flag("error", f"Issue minute '{mm:02d}' is out of range", 10)

    spd = d.get("wind_speed")
    gust = d.get("wind_gust") or 0
    if spd is not None and gust:
        if gust <= spd:
            flag("error", f"Gust {gust}KT must exceed sustained wind {spd}KT", 12)
        elif gust - spd < 10:
            flag("warning", f"Gust {gust}KT is <10KT above sustained {spd}KT", 5)

    score = max(0, score)
    has_critical = any(f["severity"] == "critical" for f in findings)
    status = "FAIL" if (has_critical or score < 70) else ("REVIEW" if (findings or score < 90) else "PASS")
    return {"status": status, "score": score, "findings": findings}

def check_regional_performance(primary_score, nearby_icaos_str):
    """Fetches and scores nearby stations to compare against the primary score."""
    if not nearby_icaos_str:
        return None, None
    icaos = [i.strip().upper() for i in nearby_icaos_str.split(',') if len(i.strip()) == 4]
    if not icaos:
        return None, None
    scores = []
    for icao in icaos:
        raw = fetch_taf(icao)
        if raw:
            parsed = parse_taf(raw)
            qc = qc_process(parsed)
            scores.append(qc["score"])
    if not scores:
        return None, None
    avg_score = int(sum(scores) / len(scores))
    flag = "UNDERPERFORMING" if primary_score <= (avg_score - 10) else "NOMINAL"
    return avg_score, flag

# ==========================================
# 🌐 WEB ROUTES
# ==========================================

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")

@app.route("/api/process", methods=["POST"])
def api_process():
    payload = request.get_json(force=True, silent=True) or {}
    mode = payload.get("mode", "live")
    nearby_str = payload.get("nearby_icaos", "")
    
    if mode == "live":
        icao = (payload.get("icao") or "").strip().upper()
        raw = fetch_taf(icao)
        if not raw:
            return jsonify({"ok": False, "error": f"No TAF returned for '{icao}'."}), 200
        data = parse_taf(raw)
        source = "LIVE"
    else:
        data = build_manual_record(payload)
        source = "MANUAL"

    qc = qc_process(data)
    reg_avg, reg_flag = check_regional_performance(qc["score"], nearby_str)
    
    if reg_flag:
        severity = "warning" if reg_flag == "UNDERPERFORMING" else "info"
        qc["findings"].append({
            "severity": severity,
            "message": f"Regional Avg: {reg_avg}/100. Station Status: {reg_flag}."
        })
        if reg_flag == "UNDERPERFORMING" and qc["status"] == "PASS":
            qc["status"] = "REVIEW"

    rowid = insert_log(data, qc, source, reg_avg, reg_flag)
    return jsonify({"ok": True, "id": rowid, "source": source, "parsed": data, "qc": qc})

def build_manual_record(p):
    wdir = (p.get("wind_dir") or "").strip().upper()
    try: spd = int(p.get("wind_speed") or 0)
    except: spd = 0
    try: gust = int(p.get("wind_gust") or 0)
    except: gust = 0
    gust_str = f"G{gust:02d}" if gust else ""
    wind = f"{wdir}{spd:02d}{gust_str}KT" if wdir else None
    return {
        "type": "TAF",
        "icao": (p.get("icao") or "").strip().upper() or None,
        "issue_time": (p.get("issue_time") or "").strip().upper() or None,
        "validity": (p.get("validity") or "").strip() or None,
        "wind": wind, "wind_dir": wdir or None, "wind_speed": spd, "wind_gust": gust,
        "visibility": (p.get("visibility") or "").strip() or None,
        "cloud_layers": (p.get("cloud_layers") or "").strip() or None,
        "weather_phenomena": (p.get("weather_phenomena") or "").strip() or None,
        "altimeter": (p.get("altimeter") or "").strip() or None,
        "change_groups": None, "raw_text": "MANUAL_ENTRY"
    }

@app.route("/api/logs")
def api_logs():
    return jsonify({"ok": True, "logs": fetch_logs()})

@app.route("/api/export")
def api_export():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        export_csv(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=taf_logs_{stamp}.csv"}
    )

# ==========================================
# 🖥️ FRONTEND INTERFACE
# ==========================================
PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TAF QC Console</title>
<style>
:root{
--bg:#0b1220; --panel:#111c2e; --panel2:#0e1726; --line:#22324a;
--ink:#e6edf6; --muted:#8090a8; --amber:#f5a623; --cyan:#56b4e0;
--pass:#3ddc84; --review:#f5a623; --fail:#ff5d5d;
--mono:"JetBrains Mono",monospace;
--sans:system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5}
.wrap{max-width:1060px;margin:0 auto;padding:0 20px 64px}
header{border-bottom:1px solid var(--line);background:linear-gradient(180deg,#0e1828,#0b1220);position:sticky;top:0;z-index:5}
.bar{max-width:1060px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.brand b{font-size:18px;letter-spacing:.06em}
.tag{font-family:var(--mono);font-size:11px;color:var(--amber);border:1px solid var(--amber);border-radius:3px;padding:2px 7px}
.clock{margin-left:auto;font-family:var(--mono);color:var(--cyan);font-size:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px 20px;margin-top:18px}
.modes{display:flex;gap:6px;margin-bottom:16px}
.modes button{background:transparent;border:1px solid var(--line);color:var(--muted);padding:7px 16px;border-radius:6px;cursor:pointer;font-family:var(--mono)}
.modes button.on{color:var(--bg);background:var(--amber);border-color:var(--amber)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
label{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:5px}
input{background:var(--panel2);border:1px solid var(--line);color:var(--ink);font-family:var(--mono);font-size:15px;padding:9px 11px;border-radius:6px;width:100%}
.field{flex:1 1 130px}
.field.icao{flex:0 0 130px}
.go{background:var(--cyan);color:#04121d;border:none;font-weight:700;font-family:var(--mono);padding:10px 22px;border-radius:6px;cursor:pointer}
.manual{display:none}
.manual.on{display:block}
.grid3{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
#out{display:none}
.raw{font-family:var(--mono);font-size:14px;background:#070d18;border:1px solid var(--line);border-left:3px solid var(--cyan);border-radius:6px;padding:14px 16px;white-space:pre-wrap}
.verdict{display:flex;align-items:center;gap:18px;margin:16px 0 6px}
.badge{font-family:var(--mono);font-weight:700;padding:8px 18px;border-radius:6px;border:1px solid}
.b-PASS{color:var(--pass);border-color:var(--pass);background:rgba(61,220,132,.08)}
.b-REVIEW{color:var(--review);border-color:var(--review);background:rgba(245,166,
