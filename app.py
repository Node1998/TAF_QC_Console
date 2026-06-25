# -*- coding: utf-8 -*-


import os
import re
import csv
import sqlite3
from io import StringIO
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, Response

APP_DIR = os.getcwd()
# Use /data/ directory if on Render (for persistent disk), otherwise local directory
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "taf_validation.db"))
AWC_URL = "https://aviationweather.gov/api/data/taf"

app = Flask(__name__)

# Basic Weather Phenomena regex placeholder to prevent crashes
_WX = r"VC|MI|BC|PR|DR|BL|SH|TS|FZ|DZ|RA|SN|SG|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS"

def init_db():
    """Create the log table if it does not yet exist."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS taf_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT,
            report_type TEXT,
            issue_time TEXT,
            validity TEXT,
            wind TEXT,
            wind_speed INTEGER,
            wind_gust INTEGER,
            visibility TEXT,
            cloud_layers TEXT,
            weather_phenomena TEXT,
            altimeter TEXT,
            change_groups TEXT,
            qc_status TEXT,
            qc_score INTEGER,
            source TEXT,
            raw_text TEXT,
            logged_at TEXT
        )
    """)
    conn.commit()
    conn.close()
def upgrade_db():
    """Safely adds new regional comparison columns to an existing database."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("ALTER TABLE taf_logs ADD COLUMN regional_avg INTEGER")
        conn.execute("ALTER TABLE taf_logs ADD COLUMN regional_flag TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass # Columns already exist
    conn.close()

def check_regional_performance(primary_score, nearby_icaos_str):
    """Fetches nearby TAFs, averages their scores, and compares to primary."""
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
    
    # Flag if the primary score is 10 or more points below the regional average
    if primary_score <= (avg_score - 10):
        flag = "UNDERPERFORMING"
    else:
        flag = "NOMINAL"
        
    return avg_score, flag


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
    rows = conn.execute(
        "SELECT * FROM taf_logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
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

def fetch_taf(icao):
    """Return the raw TAF string for an ICAO, or None on failure."""
    icao = (icao or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4}", icao):
        return None
    try:
        resp = requests.get(
            AWC_URL, params={"ids": icao, "format": "raw"}, timeout=10
        )
        resp.raise_for_status()
        text = resp.text.strip()
        return text or None
    except Exception:
        return None

def parse_taf(clean):
    parsed = {}
    parsed["type"] = "TAF"

    # Simple station extraction
    icao_m = re.search(r"\b([A-Z0-9]{4})\b", clean)
    parsed["icao"] = icao_m.group(1) if icao_m else None

    # Simple issue time extraction (e.g., 251145Z)
    time_m = re.search(r"\b(\d{6}Z)\b", clean)
    parsed["issue_time"] = time_m.group(1) if time_m else None

    # Validity period extraction (e.g., 2512/2618)
    valid_m = re.search(r"\b(\d{4}/\d{4})\b", clean)
    parsed["validity"] = valid_m.group(1) if valid_m else None

    # Wind Extraction
    wind_m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b", clean)
    if wind_m:
        parsed["wind"] = wind_m.group(0)
        parsed["wind_dir"] = wind_m.group(1)
        parsed["wind_speed"] = int(wind_m.group(2))
        parsed["wind_gust"] = int(wind_m.group(3)) if wind_m.group(3) else 0
    else:
        parsed["wind"] = None
        parsed["wind_dir"] = None
        parsed["wind_speed"] = None
        parsed["wind_gust"] = 0

    region = clean[wind_m.end():] if wind_m else clean
    vis_m = re.search(
        r"\b(CAVOK|P6SM|M?\d+\s+\d+/\d+SM|M?\d+/\d+SM|M?\d{1,2}SM|\d{4})\b", region)
    parsed["visibility"] = vis_m.group(1) if vis_m else None

    alt_m = re.search(r"\bQNH(\d{4})INS\b", clean) or re.search(r"\bA(\d{4})\b", clean)
    parsed["altimeter"] = alt_m.group(1) if alt_m else None

    clouds = re.findall(
        r"\b((?:FEW|SCT|BKN|OVC|VV)\d{3}(?:CB|TCU)?|SKC|CLR|NSC|NCD)\b", clean)
    parsed["cloud_layers"] = ", ".join(clouds) if clouds else None

    wx = re.findall(r"\b(" + _WX + r")\b", clean)
    parsed["weather_phenomena"] = ", ".join(wx) if wx else None

    cg = re.findall(r"\b(BECMG|TEMPO|FM\d{6}|PROB\d{2})", clean)
    parsed["change_groups"] = ", ".join(cg) if cg else None

    parsed["raw_text"] = clean
    return parsed

def qc_process(d):
    """Validate parsed TAF data, returning status, 0-100 score, and findings."""
    findings = []
    score = 100
    def flag(severity, message, penalty):
        nonlocal score
        findings.append({"severity": severity, "message": message})
        score -= penalty

    mandatory = {
        "type": "Report type (TAF)",
        "icao": "Station identifier",
        "issue_time": "Issue time",
        "validity": "Valid period",
        "wind": "Wind group",
        "visibility": "Visibility",
    }
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
            if not 1 <= dd <= 31:
                flag("error", f"Issue day '{dd:02d}' is out of range (01-31)", 10)
            if hh > 23:
                flag("error", f"Issue hour '{hh:02d}' is out of range (00-23)", 10)
            if mm > 59:
                flag("error", f"Issue minute '{mm:02d}' is out of range (00-59)", 10)

    if d.get("validity") and not re.fullmatch(r"\d{4}/\d{4}", d["validity"]):
        flag("error", "Valid period must be DDHH/DDHH (e.g. 1215/1321)", 10)

    spd = d.get("wind_speed")
    gust = d.get("wind_gust") or 0
    wdir = d.get("wind_dir")
    if wdir and wdir != "VRB":
        try:
            if int(wdir) > 360:
                flag("error", f"Wind direction {wdir}° exceeds 360°", 10)
        except ValueError:
            pass
    if spd is not None:
        if spd > 100:
            flag("warning", f"Sustained wind {spd}KT is extreme — verify the encode", 8)
        if gust:
            if gust <= spd:
                flag("error", f"Gust {gust}KT must exceed sustained wind {spd}KT", 12)
            elif gust - spd < 10:
                flag("warning", f"Gust {gust}KT is <10KT above sustained {spd}KT — gusts are normally coded only when ≥10KT above the mean", 5)

    vis = d.get("visibility")
    if vis and vis not in ("9999", "CAVOK", "P6SM"):
        m = re.fullmatch(r"(\d{4})", vis)
        if m and int(m.group(1)) < 1000 and not d.get("weather_phenomena"):
            flag("warning", f"Visibility {int(m.group(1))}m is low but no obscuration (FG/BR/precip) is coded", 8)

    score = max(0, score)
    has_critical = any(f["severity"] == "critical" for f in findings)
    if has_critical or score < 70:
        status = "FAIL"
    elif findings or score < 90:
        status = "REVIEW"
    else:
        status = "PASS"
    return {"status": status, "score": score, "findings": findings}

# Note: The raw HTML from your PAGE variable goes here.
# (Make sure the javascript `fetch` targets remain relative, like "/api/process", which they already are!)
PAGE = """<!DOCTYPE html>
<div id="liveForm" class="row">
    <div class="field icao">
   <div><label>Nearby Stations (Comma separated)</label><input id="nearby_manual" placeholder="KEDW, KNLS"></div>
        <label>Station ICAO</label>
        <input id="icao" value="KLSV" maxlength="4" placeholder="KLSV" autocomplete="off" oninput="this.value=this.value.toUpperCase()">
    </div>
    <div class="field">
        <label>Nearby Stations (Comma separated)</label>
        <input id="nearby_live" placeholder="KEDW, KNLS, KTNX" autocomplete="off" oninput="this.value=this.value.toUpperCase()">
    </div>
    <button class="go" id="goLive" onclick="process()">Process</button>
</div>
*{
box-sizing:border-box}
body{
margin:0;
background:var(--bg);
color:var(--ink);
font-family:var(--sans);
font-size:15px;
line-height:1.5}
a{color:var(--cyan)}
.wrap{
max-width:1060px;
margin:0 auto;
padding:0 20px 64px}
header{
border-bottom:1px solid var(--line);
background:
linear-gradient(180deg,#0e1828,#0b1220);
position:sticky;
top:0;z-index:5}
.bar{
max-width:1060px;
margin:0 auto;
padding:14px 20px;
display:flex;
align-items:center;
gap:16px;
flex-wrap:wrap}
.brand{
display:flex;
align-items:baseline;
gap:10px}
.brand b{
font-size:18px;
letter-spacing:.06em}
.tag{
font-family:var(--mono);
font-size:11px;
color:var(--amber);
border:1px solid var(--amber);
border-radius:3px;
padding:2px 7px;
letter-spacing:.08em}
.clock{
margin-left:auto;
font-family:var(--mono);
color:var(--cyan);
font-size:14px;
letter-spacing:.05em}
.clock span{
color:var(--muted);
font-size:11px;
margin-right:6px}
h2{
font-size:12px;
letter-spacing:.14em;
text-transform:uppercase;
color:var(--muted);
font-weight:600;
margin:0 0 12px}
.panel{
background:var(--panel);
border:1px solid var(--line);
border-radius:8px;
padding:18px 20px;
margin-top:18px}
.modes{
display:flex;
gap:6px;
margin-bottom:16px}
.modes button{
flex:0 0 auto;
background:transparent;
border:1px solid var(--line);
color:var(--muted);
padding:7px 16px;
border-radius:6px;
cursor:pointer;
font-family:var(--mono);
font-size:12px;
letter-spacing:.06em}
.modes button.on{
color:var(--bg);
background:var(--amber);
border-color:var(--amber)}
.row{
display:flex;
gap:10px;
flex-wrap:wrap;
align-items:flex-end}
label{
display:block;
font-size:11px;
letter-spacing:.06em;
color:var(--muted);
text-transform:uppercase;
margin-bottom:5px}
input{
background:var(--panel2);
border:1px solid var(--line);
color:var(--ink);
font-family:var(--mono);
font-size:15px;
padding:9px 11px;
border-radius:6px;
width:100%}
input:focus{
outline:none;
border-color:var(--cyan);
box-shadow:0 0 0 2px rgba(86,180,224,.18)}
.field{
flex:1 1 130px}
.field.icao{
flex:0 0 130px}
.go{
flex:0 0 auto;
background:var(--cyan);
color:#04121d;
border:none;
font-weight:700;
font-family:var(--mono);
letter-spacing:.06em;
padding:10px 22px;
border-radius:6px;
cursor:pointer;
font-size:13px}
.go:disabled{
opacity:.5;
cursor:default}
.manual{
display:none}
.manual.on{
display:block}
.grid3{
display:grid;
grid-template-columns:repeat(3,1fr);
gap:10px}
#out{
display:none}
.raw{
font-family:var(--mono);
font-size:14px;
background:#070d18;
border:1px solid var(--line);
border-left:3px solid var(--cyan);
border-radius:6px;
padding:14px 16px;
white-space:pre-wrap;
word-break:break-word;
color:#cfe2f2}
.verdict{
display:flex;
align-items:center;
gap:18px;
margin:16px 0 6px}
.badge{
font-family:var(--mono);
font-weight:700;
letter-spacing:.1em;
font-size:15px;
padding:8px 18px;
border-radius:6px;
border:1px solid}
.b-PASS{\\n",
color:var(--pass);
border-color:var(--pass);
background:rgba(61,220,132,.08)}
.b-REVIEW{
color:var(--review);
border-color:var(--review);
background:rgba(245,166,35,.08)}
.b-FAIL{
color:var(--fail);
border-color:var(--fail);
background:rgba(255,93,93,.08)}
.gauge{
flex:1 1 auto}
.gauge .nums{
display:flex;
justify-content:space-between;
font-family:var(--mono);
font-size:12px;
color:var(--muted);
margin-bottom:5px}
.gauge .nums b{
color:var(--ink);
font-size:15px}
.track{
height:8px;
background:#0a1322;
border-radius:5px;
overflow:hidden}
.fill{
height:100%;
width:0;
border-radius:5px;
transition:width .6s ease}
.groups{
display:grid;
grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
gap:1px;
background:var(--line);
border:1px solid var(--line);
border-radius:6px;
overflow:hidden;
margin-top:16px}
.cell{
background:var(--panel2);
padding:10px 12px}
.cell .k{
font-size:10px;
letter-spacing:.08em;
text-transform:uppercase;
color:var(--muted)}
.cell .v{
font-family:var(--mono);
font-size:14px;
margin-top:3px;
color:var(--ink);
word-break:break-word}
.cell .v.none{
color:#4a5a72}
.findings{
margin-top:16px;
display:flex;
flex-direction:column;
gap:7px}
.find{
display:flex;
gap:10px;
align-items:flex-start;
font-size:13.5px;
padding:8px 12px;
border-radius:6px;
background:var(--panel2);
border-left:3px solid var(--line)}
.find .sev{
font-family:var(--mono);
font-size:10px;
letter-spacing:.08em;
padding:2px 6px;
border-radius:3px;
flex:0 0 auto;
margin-top:1px}
.find.critical{
border-left-color:var(--fail)}
.find.critical .sev{
color:var(--fail);
background:rgba(255,93,93,.12)}
.find.error{
border-left-color:var(--fail)}
.find.error .sev{
color:var(--fail);
background:rgba(255,93,93,.12)}
.find.warning{
border-left-color:var(--review)}
.find.warning .sev{
color:var(--review);
background:rgba(245,166,35,.12)}
.clean{
color:var(--pass);
font-family:var(--mono);
font-size:13px;
margin-top:14px}
.loghead{
display:flex;
align-items:center;
gap:12px}
.loghead .btn{
margin-left:auto}
.btn{
background:transparent;
border:1px solid var(--line);
color:var(--ink);
padding:7px 14px;
border-radius:6px;
cursor:pointer;
font-family:var(--mono);
font-size:12px;
letter-spacing:.05em}
.btn:hover{
border-color:var(--cyan)}
.btn.exp{
border-color:var(--amber);
color:var(--amber)}
table{
width:100%;
border-collapse:collapse;
margin-top:12px;
font-size:13px}
th{
font-size:10px;
letter-spacing:.08em;
text-transform:uppercase;
color:var(--muted);
text-align:left;
padding:8px 10px;
border-bottom:1px solid var(--line)}
td{
padding:9px 10px;
border-bottom:1px solid #16243a;
font-family:var(--mono)}
tr:hover td{
background:#0e1828}
.pill{
font-size:11px;
font-weight:700;
padding:2px 8px;
border-radius:4px}
.pill.PASS{
color:var(--pass);
background:rgba(61,220,132,.12)}
.pill.REVIEW{
color:var(--review);
border-color:var(--review);
background:rgba(245,166,35,.12)}
.pill.FAIL{
color:var(--fail);
border-color:var(--fail);
background:rgba(255,93,93,.12)}
.empty{
color:var(--muted);
font-family:var(--mono);
font-size:13px;
padding:18px 4px}
.src{
color:var(--muted);
font-size:11px}
@media(max-width:620px){.grid3{grid-template-columns:1fr 1fr}
.field.icao{flex:1 1 100%}.clock{display:none}
th.hide,td.hide{display:none}}
</style>
</head>
<body>
<header>
<div class="bar">
<div class="brand"><b>TAF QC CONSOLE</b><span class="tag">AFMAN 15-124</span></div>
<div class="clock"><span>ZULU</span><b id="zulu">--:--:--</b></div>
</div>
</header>
<div class="wrap">
<!-- INGEST -->
<section class="panel">
<h2>Ingest forecast</h2>
<div class="modes">
<button id="m-live" class="on" onclick="setMode('live')">Fetch live</button>
<button id="m-manual" onclick="setMode('manual')">Manual entry</button>
</div>
<div id="liveForm" class="row">
<div class="field icao">
<label>Station ICAO</label>
<input id="icao" value="KLSV" maxlength="4" placeholder="KLSV" autocomplete="off" oninput="this.value=this.value.toUpperCase()" onkeydown="if(event.key==='Enter')process()">
</div>
<button class="go" id="goLive" onclick="process()">Process</button>
</div>
<div id="manualForm" class="manual">
<div class="grid3">
<div><label>ICAO</label><input id="m_icao" placeholder="KEDW"></div>
<div><label>Issue time</label><input id="m_issue" placeholder="121500Z"></div>
<div><label>Valid period</label><input id="m_valid" placeholder="1215/1315"></div>
<div><label>Wind dir</label><input id="m_wdir" placeholder="270"></div>
<div><label>Wind speed (KT)</label><input id="m_wspd" placeholder="10"></div>
<div><label>Gust (KT, 0=none)</label><input id="m_wgst" placeholder="0"></div>
<div><label>Visibility</label><input id="m_vis" placeholder="9999 or 7SM"></div>
<div><label>Clouds</label><input id="m_cld" placeholder="SCT250, BKN100"></div>
<div><label>Weather</label><input id="m_wx" placeholder="BR, -RA"></div>
<div><label>Altimeter</label><input id="m_alt" placeholder="2992"></div>
</div>
<div style="margin-top:14px"><button class="go" onclick="process()">Process</button></div>
</div>
</section>
<!-- RESULT -->
<section class="panel" id="out">
<h2>QC result</h2>
<div class="raw" id="raw"></div>
<div class="verdict">
<div class="badge" id="badge"></div>
<div class="gauge">
<div class="nums"><span>Quality score</span><b><span id="score">0</span>/100</b></div>
<div class="track"><div class="fill" id="fill"></div></div>
</div>
</div>
<div class="groups" id="groups"></div>
<div id="findwrap"></div>
</section>
<!-- LOG -->
<section class="panel">
<div class="loghead">
<h2 style="margin:0">Validation log</h2>
<button class="btn" onclick="loadLogs()">Refresh</button>
<button class="btn exp" onclick="location.href='/api/export'">Export CSV</button>
</div>
<div id="logbox"><div class="empty">Loading...</div></div>
</section>
</div>
<script>
let mode = "live";
function setMode(m){
mode = m;
document.getElementById("m-live").classList.toggle("on", m==="live");
document.getElementById("m-manual").classList.toggle("on", m==="manual");
document.getElementById("liveForm").style.display = m==="live" ? "flex":"none";
document.getElementById("manualForm").classList.toggle("on", m==="manual");
}
function tick(){
const d=new Date();
const p=n=>String(n).padStart(2,"0");
document.getElementById("zulu").textContent = p(d.getUTCHours())+":"+p(d.getUTCMinutes())+":"+p(d.getUTCSeconds());
}
setInterval(tick,1000); tick();
const LABELS = {
type:"Type", icao:"Station", issue_time:"Issue", validity:"Valid",
wind:"Wind", visibility:"Visibility", cloud_layers:"Clouds",
weather_phenomena:"Weather", altimeter:"Altimeter",
change_groups:"Change grps"
};
async function process(){
    const body = {mode};
    if(mode==="live"){
        body.icao = document.getElementById("icao").value.trim().toUpperCase();
        body.nearby_icaos = document.getElementById("nearby_live").value.trim();
        if(body.icao.length!==4){ alert("Enter a 4-letter ICAO code."); return; }
    } else {
        body.icao=v("m_icao"); body.issue_time=v("m_issue");
        body.validity=v("m_valid");
        body.wind_dir=v("m_wdir"); body.wind_speed=v("m_wspd");
        body.wind_gust=v("m_wgst");
        body.visibility=v("m_vis"); body.cloud_layers=v("m_cld");
        body.weather_phenomena=v("m_wx"); body.altimeter=v("m_alt");
        body.nearby_icaos=v("nearby_manual");
    }
setBusy(true);
try{
const r = await fetch("/api/process",{method:"POST", headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
const j = await r.json();
if(!j.ok){ alert(j.error||"Processing failed."); return; }
render(j); loadLogs();
}catch(e){ alert("Request failed: "+e); }
finally{ setBusy(false); }
}
const v = id => document.getElementById(id).value;
function setBusy(b){ document.getElementById("goLive").disabled=b; }
function render(j){
const out=document.getElementById("out"); out.style.display="block";
document.getElementById("raw").textContent = j.parsed.raw_text || "—";
const badge=document.getElementById("badge");
badge.textContent=j.qc.status; badge.className="badge b-"+j.qc.status;
document.getElementById("score").textContent=j.qc.score;
const fill=document.getElementById("fill");
const col = j.qc.status==="PASS"?getCss("--pass") : j.qc.status==="REVIEW"?getCss("--review"):getCss("--fail");
fill.style.background=col; fill.style.width=j.qc.score+"%";
const g=document.getElementById("groups"); g.innerHTML="";
Object.keys(LABELS).forEach(k=>{
const val=j.parsed[k];
g.insertAdjacentHTML("beforeend", `<div class="cell"><div class="k">${LABELS[k]}</div><div class="v ${val?"":"none"}">${val?esc(val):"—"}</div></div>`);
});
const fw=document.getElementById("findwrap");
if(!j.qc.findings.length){
fw.innerHTML='<div class="clean">✓ No discrepancies — all checked groups compliant.</div>';
} else {
fw.innerHTML='<div class="findings">'+j.qc.findings.map(f=>`<div class="find ${f.severity}"><span class="sev">${f.severity.toUpperCase()}</span><span>${esc(f.message)}</span></div>`).join("")+'</div>';
}
out.scrollIntoView({behavior:"smooth",block:"nearest"});
}
async function loadLogs(){
const box=document.getElementById("logbox");
try{
const j=await (await fetch("/api/logs")).json();
if(!j.logs.length){ box.innerHTML='<div class="empty">No forecasts logged yet.</div>'; return; }
let h='<table><thead><tr><th>ID</th><th>Station</th><th>Issue</th><th class="hide">Wind</th><th class="hide">Vis</th><th>Score</th><th>Status</th><th class="hide">Source</th><th class="hide">Logged (Z)</th></tr></thead><tbody>';
j.logs.forEach(r=>{
h+=`<tr><td>${r.id}</td><td>${esc(r.icao||"—")}</td><td>${esc(r.issue_time||"—")}</td><td class="hide">${esc(r.wind||"—")}</td><td class="hide">${esc(r.visibility||"—")}</td><td>${r.qc_score}</td><td><span class="pill ${r.qc_status}">${r.qc_status}</span></td><td class="hide src">${esc(r.source||"")}</td><td class="hide src">${esc(r.logged_at||"")}</td></tr>`;
});
box.innerHTML=h+"</tbody></table>";
}catch(e){ box.innerHTML='<div class="empty">Could not load logs.</div>'; }
}
const esc=s=>String(s).replace(/[&<>\"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
const getCss=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
loadLogs();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")

@app.route("/api/process", methods=["POST"])
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
    
    # Run the regional comparison
    reg_avg, reg_flag = check_regional_performance(qc["score"], nearby_str)
    if reg_flag:
        qc["findings"].append({
            "severity": "warning" if reg_flag == "UNDERPERFORMING" else "info",
            "message": f"Regional Avg Score: {reg_avg}/100. Station Status: {reg_flag}."
        })
        # Optionally adjust the final status to REVIEW if it is underperforming
        if reg_flag == "UNDERPERFORMING" and qc["status"] == "PASS":
            qc["status"] = "REVIEW"

    rowid = insert_log(data, qc, source, reg_avg, reg_flag)
    return jsonify({"ok": True, "id": rowid, "source": source, "parsed": data, "qc": qc})

# At the bottom of your script, ensure BOTH db functions run:
init_db()
upgrade_db() # Adds new columns to existing databases

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)


def build_manual_record(p):
    wdir = (p.get("wind_dir") or "").strip().upper()
    try:
        spd = int(p.get("wind_speed") or 0)
    except (ValueError, TypeError):
        spd = 0
    try:
        gust = int(p.get("wind_gust") or 0)
    except (ValueError, TypeError):
        gust = 0
    gust_str = f"G{gust:02d}" if gust else ""
    wind = f"{wdir}{spd:02d}{gust_str}KT" if wdir else None
    return {
        "type": "TAF",
        "icao": (p.get("icao") or "").strip().upper() or None,
        "issue_time": (p.get("issue_time") or "").strip().upper() or None,
        "validity": (p.get("validity") or "").strip() or None,
        "wind": wind,
        "wind_dir": wdir or None,
        "wind_speed": spd,
        "wind_gust": gust,
        "visibility": (p.get("visibility") or "").strip() or None,
        "cloud_layers": (p.get("cloud_layers") or "").strip() or None,
        "weather_phenomena": (p.get("weather_phenomena") or "").strip() or None,
        "altimeter": (p.get("altimeter") or "").strip() or None,
        "change_groups": None,
        "raw_text": "MANUAL_ENTRY",
    }

@app.route("/api/logs")
def api_logs():
    return jsonify({"ok": True, "logs": fetch_logs()})

@app.route("/api/export")
def api_export():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        export_csv(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=taf_logs_{stamp}.csv"
        },
    )

# Run initialization inside python script
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
