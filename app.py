import os
import re
import csv
import sqlite3
from io import StringIO
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, Response

# ==========================================
# ⚙️ CONSTANTS & APP INITIALIZATION
# ==========================================
APP_DIR = os.getcwd()
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "taf_validation.db"))
AWC_URL = "https://aviationweather.gov/api/data/taf"
app = Flask(__name__)
_WX = r"VC|MI|BC|PR|DR|BL|SH|TS|FZ|DZ|RA|SN|SG|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS"

# ==========================================
# 🗄️ STORAGE LAYER (DATABASE)
# ==========================================
def init_db():
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
# ✈️ TAF PARSING & QC LOGIC
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
        parsed["wind"], parsed["wind_dir"], parsed["wind_speed"] = wind_m.group(0), wind_m.group(1), int(wind_m.group(2))
        parsed["wind_gust"] = int(wind_m.group(3)) if wind_m.group(3) else 0
    else:
        parsed["wind"], parsed["wind_dir"], parsed["wind_speed"], parsed["wind_gust"] = None, None, None, 0
    region = clean[wind_m.end():] if wind_m else clean
    vis_m = re.search(r"\b(CAVOK|P6SM|M?\d+\s+\d+/\d+SM|M?\d+/\d+SM|M?\d{1,2}SM|\d{4})\b", region)
    parsed["visibility"] = vis_m.group(1) if vis_m else None
    clouds = re.findall(r"\b((?:FEW|SCT|BKN|OVC|VV)\d{3}(?:CB|TCU)?|SKC|CLR|NSC|NCD)\b", clean)
    parsed["cloud_layers"] = ", ".join(clouds) if clouds else None
    return parsed

def qc_process(d):
    findings, score = [], 100
    def flag(sev, msg, pen):
        nonlocal score
        findings.append({"severity": sev, "message": msg})
        score -= pen
    mandatory = {"icao": "Station identifier", "issue_time": "Issue time", "validity": "Valid period", "wind": "Wind group"}
    for key, label in mandatory.items():
        if not d.get(key):
            flag("critical", f"Missing mandatory group: {label}", 25)
    if d.get("wind_speed") is not None and d.get("wind_gust", 0) > 0 and d.get("wind_gust") <= d.get("wind_speed"):
        flag("error", f"Gust {d['wind_gust']}KT must exceed sustained wind {d['wind_speed']}KT", 15)
    score = max(0, score)
    status = "FAIL" if any(f["severity"] == "critical" for f in findings) or score < 70 else "REVIEW" if findings or score < 90 else "PASS"
    return {"status": status, "score": score, "findings": findings}

def check_regional_performance(primary_score, nearby_icaos_str):
    if not nearby_icaos_str: return None, None
    icaos = [i.strip().upper() for i in nearby_icaos_str.split(',') if len(i.strip()) == 4]
    if not icaos: return None, None
    scores = [qc_process(parse_taf(raw))["score"] for icao in icaos if (raw := fetch_taf(icao))]
    if not scores: return None, None
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
def api_process(
