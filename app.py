import os
import re
import sqlite3
import json
import requests
import pandas as pd
from flask import Flask, request, jsonify, render_template, send_file
from io import BytesIO

app = Flask(__name__)
DB_PATH = "taf_validation.db"

# --- Initialization & Database ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS taf_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT, status TEXT, score INTEGER, findings TEXT, raw_taf TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- AFMAN 15-124 QC ENGINE ---
WX_DESCRIPTOR = "MI|PR|BC|DR|BL|SH|TS|FZ"
WX_PRECIP = "DZ|RA|SN|SG|IC|PL|GR|GS|UP"
WX_OBSCURE = "BR|FG|FU|VA|DU|SA|HZ|PY"
WX_OTHER = "PO|SQ|FC|SS|DS"
RE_TAF_HEAD = re.compile(r"^TAF(?:\s+(AMD|COR))?\b")
RE_ICAO = re.compile(r"^[A-Z][A-Z0-9]{3}$")
RE_WX_HASPHEN = re.compile(rf"(?:{WX_DESCRIPTOR}|{WX_PRECIP}|{WX_OBSCURE}|{WX_OTHER})")
RE_WX_STRICT = re.compile(rf"^(VC|[+-])?(?:{WX_DESCRIPTOR})?(?:{WX_PRECIP}){{0,3}}(?:{WX_OBSCURE})?(?:{WX_OTHER})?$")

def qc_taf(raw):
    findings = []
    score = 100
    text = " ".join(raw.upper().split())
    tokens = text.split()
    
    if not tokens: return "FAIL", 0, ["Empty input."]
    
    if not RE_TAF_HEAD.match(text): 
        findings.append("Message must begin with TAF.")
        score -= 15
        
    for t in tokens:
        if RE_WX_HASPHEN.search(t) and not RE_WX_STRICT.match(t):
            findings.append(f"Malformed WX group: {t}")
            score -= 5
            
    status = "PASS" if score >= 90 else ("REVIEW" if score >= 70 else "FAIL")
    return status, max(0, score), findings

# --- API Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/validate', methods=['POST'])
def validate_taf():
    data = request.json
    icao = data.get('icao', '').strip().upper()
    raw = data.get('manual_text', '').strip()
    
    try:
        if not raw and icao:
            res = requests.get(f"https://aviationweather.gov/api/data/taf?ids={icao}", timeout=10)
            res.raise_for_status()
            raw = res.text.strip()
            
        if not raw:
            return jsonify({"error": "No TAF data returned from API or empty input."}), 400
            
        status, score, findings = qc_taf(raw)
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO taf_logs (icao, status, score, findings, raw_taf) VALUES (?,?,?,?,?)",
                     (icao if icao else "MANUAL", status, score, json.dumps(findings), raw))
        conn.commit()
        conn.close()
        
        return jsonify({
            "icao": icao if icao else "MANUAL", 
            "status": status, 
            "score": score,
            "findings": findings, 
            "raw": raw
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/history', methods=['GET'])
def get_history():
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT icao, score, status FROM taf_logs ORDER BY id DESC LIMIT 10", conn)
        conn.close()
        return jsonify(df.to_dict(orient='records'))
    except:
        return jsonify([])

@app.route('/api/export', methods=['GET'])
def export_csv():
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT * FROM taf_logs", conn)
        conn.close()
        
        csv_data = df.to_csv(index=False).encode('utf-8')
        return send_file(
            BytesIO(csv_data),
            mimetype='text/csv',
            as_attachment=True,
            download_name='taf_qc_history.csv'
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Render requires binding to 0.0.0.0 and dynamically pulling the PORT
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
