# @title ✈️ TAF QC Console - AFMAN 15-124 Compliance {display-mode: "form"}
import re
import json
import requests
from google.colab import output
from IPython.display import HTML

# --- QC ENGINE LOGIC (Python Port for API usage) ---
def qc_engine_logic(raw_text):
    findings = []
    score = 100
    raw = " ".join(raw_text.upper().split())

    # Validation Rules
    if not re.search(r'\b[A-Z0-9]{4}\b', raw):
        findings.append({"sev": "critical", "ref": "1.3.2.1.3", "msg": "Missing or malformed 4-letter ICAO identifier."})
        score -= 20
    if not re.search(r'\b\d{6}Z\b', raw):
        findings.append({"sev": "critical", "ref": "1.3.2.1.4", "msg": "Missing or malformed issue date/time (DDHHMMZ)."})
        score -= 15
    if not re.search(r'\b\d{4}/\d{4}\b', raw):
        findings.append({"sev": "critical", "ref": "1.3.2.1.5", "msg": "Missing or malformed valid period (DDHH/DDHH)."})
        score -= 15
    if not re.search(r'\b(VRB|\d{3})\d{2,3}(G\d{2,3})?KT\b', raw):
        findings.append({"sev": "critical", "ref": "1.3.4", "msg": "Missing mandatory wind group."})
        score -= 20
    if "QNH" not in raw and "INS" not in raw:
        findings.append({"sev": "warning", "ref": "1.3.12", "msg": "No QNH altimeter found (Standard for AF TAFs)."})
        score -= 5

    status = "PASS" if score >= 90 else ("REVIEW" if score >= 70 else "FAIL")
    return {"status": status, "score": max(0, score), "findings": findings, "raw": raw}

def process_taf_proxy(icao, manual_text=None):
    try:
        if manual_text:
            raw = manual_text
        else:
            resp = requests.get(f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw", timeout=10)
            raw = resp.text.strip() if resp.ok else ""

        if not raw:
            return json.dumps({"error": "No TAF data found."})

        result = qc_engine_logic(raw)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})

output.register_callback('process_taf_proxy', process_taf_proxy)

# --- DASHBOARD UI ---
HTML_UI = r"""
<!DOCTYPE html>
<html>
<head>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono&family=Inter:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #0b1220; --panel: #111c2e; --line: #22324a; --ink: #e6edf6; --muted: #8090a8; --cyan: #56b4e0; --pass: #3ddc84; --fail: #ff5d5d; }
        body { background: var(--bg); color: var(--ink); font-family: 'Inter', sans-serif; padding: 20px; }
        .card { background: var(--panel); border: 1px solid var(--line); padding: 20px; border-radius: 12px; max-width: 900px; margin: auto; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--line); padding-bottom: 15px; margin-bottom: 20px; }
        h2 { margin: 0; font-size: 18px; letter-spacing: 1px; color: var(--cyan); }
        .input-group { display: flex; gap: 10px; margin-bottom: 20px; }
        input, textarea { background: #070d18; border: 1px solid var(--line); color: white; padding: 12px; border-radius: 8px; width: 100%; font-family: 'JetBrains Mono', monospace; }
        button { background: var(--cyan); color: #0b1220; border: none; padding: 12px 24px; border-radius: 8px; font-weight: bold; cursor: pointer; }
        #output { display: none; margin-top: 20px; }
        .raw-box { background: #070d18; padding: 15px; border-radius: 8px; border-left: 4px solid var(--cyan); font-family: 'JetBrains Mono', monospace; margin-bottom: 20px; }
        .verdict { display: flex; align-items: center; gap: 20px; background: rgba(255,255,255,0.03); padding: 15px; border-radius: 8px; }
        .badge { padding: 5px 15px; border-radius: 5px; font-weight: bold; text-transform: uppercase; }
        .status-PASS { background: rgba(61,220,132,0.2); color: var(--pass); }
        .status-FAIL { background: rgba(255,93,93,0.2); color: var(--fail); }
        .finding { padding: 10px; border-bottom: 1px solid var(--line); font-size: 13px; display: flex; gap: 10px; }
        .sev-critical { color: var(--fail); font-weight: bold; }
        .ref { color: var(--muted); font-family: 'JetBrains Mono', monospace; min-width: 80px; }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <h2>✈️ TAF QC CONSOLE</h2>
            <span style="color: var(--muted); font-size: 12px;">AFMAN 15-124 Compliance Checker</span>
        </div>

        <div class="input-group">
            <input id="icao" placeholder="ICAO (e.g. KLSV)" style="width: 150px;" maxlength="4">
            <button onclick="run('live')">Fetch Live</button>
        </div>
        <textarea id="manual" placeholder="Or paste raw TAF here..." rows="3"></textarea>
        <button onclick="run('manual')" style="margin-top:10px; width: 100%; background: #22324a; color: white;">Validate Manual Text</button>

        <div id="output">
            <div class="raw-box" id="raw-disp"></div>
            <div class="verdict">
                <div id="badge-disp" class="badge"></div>
                <div style="font-size: 24px;">Score: <b id="score-disp"></b></div>
            </div>
            <div id="findings-disp" style="margin-top: 20px;"></div>
        </div>
    </div>

    <script>
        async function run(mode) {
            const icao = document.getElementById('icao').value.toUpperCase();
            const text = mode === 'manual' ? document.getElementById('manual').value : null;

            const outBox = document.getElementById('output');
            outBox.style.display = 'block';
            document.getElementById('raw-disp').innerText = "Processing...";

            google.colab.kernel.invokeFunction('process_taf_proxy', [icao, text], {}).then(obj => {
                const data = JSON.parse(obj.data['application/json']);
                if (data.error) {
                    document.getElementById('raw-disp').innerText = "Error: " + data.error;
                    return;
                }

                document.getElementById('raw-disp').innerText = data.raw;
                document.getElementById('badge-disp').innerText = data.status;
                document.getElementById('badge-disp').className = 'badge status-' + (data.status === 'PASS' ? 'PASS' : 'FAIL');
                document.getElementById('score-disp').innerText = data.score + "/100";

                let html = '<h3>Discrepancies</h3>';
                if (data.findings.length === 0) {
                    html += '<p style="color:var(--pass)">✓ No discrepancies found.</p>';
                } else {
                    data.findings.forEach(f => {
                        html += `<div class="finding"><span class="sev-${f.sev}">${f.sev.toUpperCase()}</span><span class="ref">${f.ref}</span><span>${f.msg}</span></div>`;
                    });
                }
                document.getElementById('findings-disp').innerHTML = html;
            });
        }
    </script>
</body>
</html>
"""
HTML(HTML_UI)
