# @title ✈️ TAF QC Console {display-mode: "form"}
import json, re, sqlite3, requests, io, csv
from datetime import datetime, timezone
from google.colab import output
from IPython.display import HTML

# --- Backend Logic ---
DB_PATH = 'taf_validation.db'
AWC_URL = 'https://aviationweather.gov/api/data/taf'

def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('CREATE TABLE IF NOT EXISTS taf_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, icao TEXT, qc_status TEXT, qc_score INTEGER, raw_text TEXT, findings TEXT, logged_at TEXT)')
        conn.commit(); conn.close()
        return "DB OK"
    except Exception as e: return str(e)

def process_taf_logic(icao, raw_text=None):
    try:
        if not raw_text:
            resp = requests.get(AWC_URL, params={'ids': icao, 'format': 'raw'}, timeout=10)
            if not resp.ok or not resp.text.strip(): return json.dumps({"error": f"No live TAF for {icao}"})
            raw_text = resp.text.strip()
        score, findings = 100, []
        raw = ' '.join(raw_text.upper().split())
        if not re.search(r'\\b([A-Z0-9]{4})\\b', raw): findings.append("Missing ICAO"); score -= 20
        if not re.search(r'\\b(\\d{6})Z\\b', raw): findings.append("Missing Issue Time"); score -= 15
        if not re.search(r'\\b(\\d{4}/\\d{4})\\b', raw): findings.append("Missing Validity Period"); score -= 20
        if not re.search(r'\\b(VRB|\\d{3})\\d{2,3}(G\\d{2,3})?KT\\b', raw): findings.append("Wind Group Error"); score -= 25
        status = 'PASS' if score >= 80 else ('REVIEW' if score >= 60 else 'FAIL')
        conn = sqlite3.connect(DB_PATH)
        conn.execute('INSERT INTO taf_logs (icao, qc_status, qc_score, raw_text, findings, logged_at) VALUES (?,?,?,?,?,?)', (icao.upper(), status, score, raw, '\\n'.join(findings), datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')))
        conn.commit(); conn.close()
        return json.dumps({"status": status, "score": score, "findings": findings, "raw": raw, "icao": icao.upper()})
    except Exception as e: return json.dumps({"error": str(e)})

def fetch_history_logic():
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute('SELECT icao, qc_status, qc_score, logged_at FROM taf_logs ORDER BY id DESC LIMIT 10').fetchall()
        conn.close()
        return json.dumps([{"icao": r[0], "status": r[1], "score": r[2], "time": r[3]} for r in rows])
    except Exception as e: return json.dumps({"error": str(e)})

def export_csv_logic():
    try:
        conn = sqlite3.connect(DB_PATH); rows = conn.execute('SELECT icao, qc_status, qc_score, raw_text, findings, logged_at FROM taf_logs ORDER BY id DESC').fetchall(); conn.close()
        output_str = io.StringIO(); writer = csv.writer(output_str); writer.writerow(['ICAO', 'Status', 'Score', 'Raw TAF', 'Findings', 'Logged At (UTC)']); writer.writerows(rows)
        return output_str.getvalue()
    except Exception as e: return f"Error: {str(e)}"

output.register_callback('process_taf', process_taf_logic)
output.register_callback('fetch_history', fetch_history_logic)
output.register_callback('export_csv', export_csv_logic)
init_db()

HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: 'Segoe UI', system-ui, sans-serif; background: #f4f6f8; padding: 20px; color: #1e293b; }
        .container { max-width: 1000px; margin: auto; }
        .header { background: #111c2e; color: white; padding: 20px; border-radius: 12px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
        .card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); margin-bottom: 20px; }
        .tab-group { display: flex; gap: 10px; margin-bottom: 15px; }
        .tab { padding: 8px 16px; cursor: pointer; border-radius: 6px; background: #e2e8f0; font-weight: 600; }
        .tab.active { background: #56b4e0; color: white; }
        .input-area { display: none; } .input-area.active { display: block; }
        input, textarea { padding: 12px; border: 1px solid #ddd; border-radius: 8px; width: 100%; box-sizing: border-box; }
        button { margin-top: 10px; padding: 12px 24px; background: #111c2e; color: white; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; width: 100%; }
        .kpi-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 20px; }
        .kpi-card { background: white; padding: 15px; border-radius: 12px; text-align: center; border-bottom: 4px solid #56b4e0; }
        .kpi-value { font-size: 24px; font-weight: bold; }
        .raw-text { background: #070d18; color: #3ddc84; padding: 15px; border-radius: 8px; font-family: monospace; white-space: pre-wrap; margin-top: 10px; min-height: 40px; }
        .status-badge { padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: bold; }
        .status-PASS { background: #dcfce7; color: #166534; } .status-FAIL { background: #fee2e2; color: #991b1b; } .status-REVIEW { background: #fef3c7; color: #92400e; }
        .history-table { width: 100%; border-collapse: collapse; } .history-table td, .history-table th { padding: 10px; border-bottom: 1px solid #f1f5f9; text-align: left; }
        #debug-log { background: #1e293b; color: #94a3b8; font-family: monospace; font-size: 11px; padding: 10px; border-radius: 8px; height: 80px; overflow-y: scroll; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>✈️ TAF QC Console</h2>
            <div style="text-align:right"><div id="time"></div><div id="conn-status">● Kernel Connected</div></div>
        </div>
        <div class="card">
            <div class="tab-group"><div class="tab active" onclick="setTab('live')">Live Fetch</div><div class="tab" onclick="setTab('manual')">Manual</div></div>
            <div id="live-input" class="input-area active"><input id="icaoIn" placeholder="ICAO (e.g. KJFK)"><button id="btn-live" onclick="validate('live')">Fetch & Analyze</button></div>
            <div id="manual-input" class="input-area"><input id="mIcao" placeholder="ICAO" style="margin-bottom:10px"><textarea id="mText" placeholder="Paste TAF..."></textarea><button id="btn-man" onclick="validate('manual')">Validate Manual</button></div>
            <button onclick="refresh()" style="background:#e2e8f0; color:#1e293b; font-size:10px; padding:4px; margin-top:5px; width:auto">Reload/Reset</button>
        </div>
        <div class="kpi-grid">
            <div class="kpi-card"><div>Status</div><div id="k-stat" class="kpi-value">--</div></div>
            <div class="kpi-card"><div>Score</div><div id="k-score" class="kpi-value">--</div></div>
            <div class="kpi-card"><div>Station</div><div id="k-icao" class="kpi-value">--</div></div>
        </div>
        <div style="display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 20px;">
            <div class="card"><h3>Results</h3><div id="rawOut" class="raw-text">No data.</div><div id="findingsOut" style="margin-top:15px"></div></div>
            <div class="card"><h3>History <button onclick="csv()" style="width:auto; padding:4px 8px; font-size:10px; float:right">Export</button></h3><table class="history-table"><tbody id="hist"></tbody></table></div>
        </div>
        <div id="debug-log">DEBUG: Console Initialized...</div>
    </div>
    <script>
        function log(msg) { const d = document.getElementById('debug-log'); d.innerHTML += `<br>[${new Date().toLocaleTimeString()}] ${msg}`; d.scrollTop = d.scrollHeight; }

        function setTab(m) {
            document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
            document.querySelectorAll('.input-area').forEach(i=>i.classList.remove('active'));
            document.querySelector(`.tab[onclick*="'${m}'"]`).classList.add('active');
            document.getElementById(`${m}-input`).classList.add('active');
        }

        async function validate(m) {
            let icao = (m==='live' ? document.getElementById('icaoIn').value : document.getElementById('mIcao').value).trim();
            let txt = m==='manual' ? document.getElementById('mText').value : null;
            if(!icao && !txt) { log("Error: No Input"); return; }

            log(`Invoking Python for ${icao}...`);
            document.getElementById('rawOut').innerText = 'Validating...';

            try {
                const r = await google.colab.kernel.invokeFunction('process_taf', [icao||'MANL', txt], {});
                const d = JSON.parse(r.data['text/plain'].slice(1,-1).replace(/\\\\/g, "\\").replace(/\\'/g, "'"));
                if(d.error) { log("Python Error: " + d.error); alert(d.error); return; }

                document.getElementById('k-stat').innerHTML = `<span class="status-badge status-${d.status}">${d.status}</span>`;
                document.getElementById('k-score').innerText = d.score;
                document.getElementById('k-icao').innerText = d.icao;
                document.getElementById('rawOut').innerText = d.raw;
                document.getElementById('findingsOut').innerHTML = d.findings.map(f=>`<div style="color:red">⚠️ ${f}</div>`).join('') || '✅ Valid';
                log("Validation Success");
                refresh();
            } catch (e) {
                log("JS Exception: " + e.message);
                document.getElementById('conn-status').innerText = '● Connection Error';
                document.getElementById('conn-status').style.color = 'red';
            }
        }

        async function refresh() {
            log("Refreshing history...");
            try {
                const r = await google.colab.kernel.invokeFunction('fetch_history', [], {});
                const logs = JSON.parse(r.data['text/plain'].slice(1,-1).replace(/\\\\/g, "\\").replace(/\\'/g, "'"));
                document.getElementById('hist').innerHTML = logs.map(l=>`<tr><td>${l.icao}</td><td>${l.score}</td><td><span class="status-badge status-${l.status}">${l.status}</span></td></tr>`).join('');
                document.getElementById('conn-status').innerText = '● Kernel Connected';
                document.getElementById('conn-status').style.color = '#3ddc84';
                log("Sync OK");
            } catch (e) { log("History Sync Failed"); }
        }

        async function csv() {
            log("Exporting CSV...");
            const r = await google.colab.kernel.invokeFunction('export_csv', [], {});
            const blob = new Blob([r.data['text/plain'].slice(1,-1).replace(/\\n/g, '\\n')], {type:'text/csv'});
            const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'taf_qc.csv'; a.click();
        }

        setInterval(() => document.getElementById('time').innerText = new Date().toUTCString(), 1000);
        refresh();
    </script>
</body>
</html>
"""
HTML(HTML_DASHBOARD)
