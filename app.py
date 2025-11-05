import os, json, time, threading, csv, io, re
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, abort, make_response, Response

# -------------------------
# App / globals
# -------------------------
app = Flask(__name__)

SERVICE_NAME = "Home Health Assistant"
START_TS = time.time()

# ENV
DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "").strip()
DEBUG_USER  = os.getenv("DEBUG_USER", "").strip()
STORE_BACKEND = os.getenv("STORE_BACKEND", "json").strip().lower()  # only "json" for now
ADMIN_PAGE_SIZE = int(os.getenv("ADMIN_PAGE_SIZE", "50"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_MAX_SIMULATE = int(os.getenv("RATE_LIMIT_MAX_SIMULATE", "30"))

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
TWILIO_READY = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID])

MODE = "live" if TWILIO_READY else "mock"

# simple in-file JSON store
STORE_FILE = "/tmp/hha_store.json"
_store_lock = threading.Lock()

def _load_store():
    with _store_lock:
        if not os.path.exists(STORE_FILE):
            data = {"messages": [], "schedules": []}
            with open(STORE_FILE, "w") as f:
                json.dump(data, f)
            return data
        with open(STORE_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                return {"messages": [], "schedules": []}

def _save_store(data):
    with _store_lock:
        with open(STORE_FILE, "w") as f:
            json.dump(data, f)

def uptime_seconds():
    return round(time.time() - START_TS, 2)

# -------------------------
# Auth helpers
# -------------------------
def _extract_token(req):
    # priority: header, query, cookie
    token = req.headers.get("X-Debug-Token", "").strip()
    if not token:
        token = req.args.get("token", "").strip()
    if not token:
        token = req.cookies.get("dbg", "").strip()
    return token

def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not DEBUG_TOKEN:
            # if no token configured, keep it open (dev)
            return fn(*args, **kwargs)
        token = _extract_token(request)
        if token and token == DEBUG_TOKEN:
            return fn(*args, **kwargs)
        return _unauthorized()
    return wrapper

def _unauthorized():
    # 401 in both HTML and JSON contexts
    if "text/html" in request.headers.get("Accept", "") and request.path.startswith("/ui"):
        html = "<h1>Unauthorized</h1><p>Provide token via ?token=...</p>"
        return make_response(html, 401)
    return make_response(jsonify({"ok": False, "error": "unauthorized"}), 401)

# -------------------------
# Small utilities
# -------------------------
def _intent_from_text(body: str) -> str:
    if not body:
        return "other"
    b = body.lower()
    if "confirm" in b or re.search(r"\byes\b", b):
        return "confirm"
    if "cancel" in b or "can't" in b or "cannot" in b:
        return "cancel"
    return "other"

# simple sliding-window limiter for simulate-sms
_limiter = {"window_start": time.time(), "count": 0}
def _check_limit():
    now = time.time()
    if now - _limiter["window_start"] > RATE_LIMIT_WINDOW_SEC:
        _limiter["window_start"] = now
        _limiter["count"] = 0
    _limiter["count"] += 1
    return _limiter["count"] <= RATE_LIMIT_MAX_SIMULATE

# -------------------------
# Core / health
# -------------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": SERVICE_NAME, "status": "ok", "mode": MODE})

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "mode": MODE,
        "service": SERVICE_NAME,
        "status": "ok",
        "twilio_ready": TWILIO_READY,
        "uptime_seconds": uptime_seconds(),
    })

@app.route("/metrics", methods=["GET"])
@app.route("/metrics.prom", methods=["GET"])
def metrics():
    # very small Prometheus sample
    s = [
        f'hha_uptime_seconds {uptime_seconds()}',
        f'hha_twilio_ready {{ready="{str(TWILIO_READY).lower()}"}} {1 if TWILIO_READY else 0}',
    ]
    return Response("\n".join(s) + "\n", mimetype="text/plain")

# -------------------------
# Debug dashboard (token optional via query or cookie)
# -------------------------
@app.route("/debug", methods=["GET"])
def debug_dash():
    token = _extract_token(request)
    status = "ok" if (not DEBUG_TOKEN or token == DEBUG_TOKEN) else "locked"
    html = f"""
    <h1>{SERVICE_NAME} Debug Dashboard</h1>
    <table border="1" cellpadding="6">
      <tr><td>MODE</td><td>{MODE}</td></tr>
      <tr><td>UPTIME (SEC)</td><td>{uptime_seconds()}</td></tr>
      <tr><td>TWILIO READY</td><td>{str(TWILIO_READY)}</td></tr>
      <tr><td>STATUS</td><td>{status}</td></tr>
    </table>
    <p>Tip: add <code>?token=YOUR_TOKEN</code> or set cookie via the schedule UI.</p>
    """
    return html

# -------------------------
# Public UI (token gate inside the page)
# -------------------------
UI_SCHEDULE = """<!doctype html>
<meta charset="utf-8"><title>Schedule</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:15px system-ui;margin:16px}
h1{margin:0 0 10px}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,button,select{padding:8px;inherit}
.card{border:1px solid #ddd;border-radius:8px;padding:10px;margin:8px 0}
.row{display:flex;justify-content:space-between;gap:8px}
.badge{background:#eef;padding:2px 6px;border-radius:6px}
.center{display:flex;min-height:40vh;align-items:center;justify-content:center}
.small{color:#666;font-size:13px}
</style>
<h1>Day Schedule</h1>

<div id="gate" class="center" style="display:none">
  <div>
    <p><b>Enter Access Token</b></p>
    <input id="tokInput" placeholder="DEBUG_TOKEN" size="40">
    <div style="height:8px"></div>
    <button onclick="saveTok()">Access</button>
  </div>
</div>

<div id="app" style="display:none">
  <div class="controls">
    <label>Date <input id="d" type="date"></label>
    <label>Therapist <input id="t" placeholder="(optional)"></label>
    <button onclick="loadDay()">Load</button>
    <button onclick="opt()">Optimize</button>
    <span class="small" id="tip"></span>
  </div>
  <div id="list"></div>
</div>

<script>
function getCookie(n){return document.cookie.split('; ').find(r=>r.startsWith(n+'='))?.split('=')[1];}
function setCookie(n,v,hrs){const d=new Date();d.setTime(d.getTime()+hrs*3600*1000);document.cookie=`${n}=${v}; expires=${d.toUTCString()}; path=/`; }
function ensureToken(){
  const urlTok=new URLSearchParams(location.search).get('token');
  if(urlTok){ setCookie('dbg',urlTok,12); history.replaceState({},'',location.pathname); return urlTok; }
  const ck=getCookie('dbg'); return ck||'';
}
function requireTokenUI(){
  const tok=ensureToken();
  if(!tok){ document.getElementById('gate').style.display='flex'; }
  else{ document.getElementById('app').style.display='block'; init(); }
}
function saveTok(){
  const v=document.getElementById('tokInput').value.trim();
  if(!v){ alert('Enter token'); return;}
  setCookie('dbg',v,12);
  location.href=location.pathname;
}
function fmt(ts){ return new Date(ts*1000).toLocaleString(); }
function init(){
  const d=document.getElementById('d');
  d.value = new Date().toISOString().slice(0,10);
  document.getElementById('tip').textContent = 'Token set in cookie for 12h';
}
async function loadDay(){
  const d=document.getElementById('d').value || new Date().toISOString().slice(0,10);
  const t=document.getElementById('t').value.trim();
  const r=await fetch(`/schedule?date=${encodeURIComponent(d)}${t?`&therapist=${encodeURIComponent(t)}`:''}`,{
    headers:{'X-Debug-Token': getCookie('dbg')||''}
  });
  if(!r.ok){ alert('Load error: ' + await r.text()); return; }
  const j=await r.json();
  const L=document.getElementById('list'); L.innerHTML='';
  (j.appointments||[]).forEach(a=>{
    const div=document.createElement('div'); div.className='card';
    div.innerHTML = `
      <div class="row"><b>${a.patient_name||'(unknown)'}</b>
      <span class="badge">${a.status||''}</span></div>
      <div class="small">${a.start_iso||''}</div>
      <div class="small">${a.address||''}, ${a.city||''} ${a.state||''} ${a.zip||''}</div>
      <div class="small">${a.lat&&a.lon?`(Lat,Lon: ${a.lat}, ${a.lon})`:''}</div>
      <div class="small">Therapist: ${a.therapist||''} &nbsp;·&nbsp; Duration: ${a.duration_min||60} min</div>
      <div class="small"><a href="https://maps.google.com/?q=${a.lat||''},${a.lon||''}" target="_blank">Map</a></div>`;
    L.appendChild(div);
  });
}
async function opt(){
  const d=document.getElementById('d').value || new Date().toISOString().slice(0,10);
  const t=document.getElementById('t').value.trim();
  const r=await fetch('/schedule/optimize',{
    method:'POST',
    headers:{'Content-Type':'application/json','X-Debug-Token': getCookie('dbg')||''},
    body: JSON.stringify({date:d, therapist:t||null})
  });
  if(!r.ok){ alert('Optimize error: ' + await r.text()); return; }
  const j=await r.json();
  alert(`Optimized. DriveTime*: ${j.drive_time? 'ON':'OFF'}`);
}
requireTokenUI();
</script>
"""

@app.route("/ui/schedule", methods=["GET"])
def ui_schedule():
    # always serve the page; the page itself will request JSON with token header/cookie
    resp = make_response(UI_SCHEDULE)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp

# -------------------------
# Data endpoints (token-protected)
# -------------------------
@app.route("/schedule", methods=["GET"])
@require_token
def schedule_get():
    date = request.args.get("date", "").strip()
    therapist = request.args.get("therapist", "").strip()
    if not date:
        # never 500—return helpful message
        return jsonify({"ok": False, "error": "missing 'date' (YYYY-MM-DD)"}), 400

    store = _load_store()
    # very simple echo: if no stored schedule for the date, return empty shell
    appts = [a for a in store.get("schedules", []) if a.get("date")==date and (not therapist or a.get("therapist","")==therapist)]
    if not appts:
        appts = []  # nothing yet (you can seed later)
    return jsonify({"status":"ok","appointments": appts})

@app.route("/schedule/optimize", methods=["POST"])
@require_token
def schedule_optimize():
    j = request.get_json(silent=True) or {}
    date = (j.get("date") or "").strip()
    therapist = (j.get("therapist") or "").strip()
    if not date:
        return jsonify({"ok": False, "error": "missing 'date'"}), 400
    # pretend we optimized; in future we’ll add routing/drive times
    return jsonify({"ok": True, "date": date, "therapist": therapist, "drive_time": False})

# -------------------------
# SMS mock + send
# -------------------------
@app.route("/simulate-sms", methods=["POST"])
def simulate_sms():
    if not _check_limit():
        return jsonify({"ok": False, "error": "rate-limit"}), 429
    j = request.get_json(silent=True) or {}
    body = (j.get("body") or "").strip()
    sender = (j.get("from") or "").strip()

    intent = _intent_from_text(body)
    msg = {
        "ts": time.time(),
        "direction": "in",
        "kind": "simulate",
        "from": sender,
        "to": "",
        "body": body,
        "intent": intent,
        "note": ""
    }
    store = _load_store()
    store["messages"].append(msg)
    _save_store(store)
    return jsonify({"ok": True, "echo": body, "intent": intent})

@app.route("/send-sms", methods=["POST"])
def send_sms():
    # still mocked until A2P is fully approved
    j = request.get_json(silent=True) or {}
    to = (j.get("to") or "").strip()
    body = (j.get("body") or "").strip()
    sid = f"mock-{int(time.time())}"
    store = _load_store()
    store["messages"].append({
        "ts": time.time(),
        "direction": "out",
        "kind": "simulate",
        "from": "",
        "to": to,
        "body": body,
        "intent": "",
        "note": "auto-confirm"
    })
    _save_store(store)
    return jsonify({"sid": sid, "status": "mock-sent"})

# -------------------------
# Admin endpoints
# -------------------------
@app.route("/admin/messages", methods=["GET"])
@require_token
def admin_messages():
    limit = int(request.args.get("limit", str(ADMIN_PAGE_SIZE)))
    q = (request.args.get("q") or "").lower().strip()

    store = _load_store()
    msgs = list(reversed(store.get("messages", [])))  # newest first
    if q:
        msgs = [m for m in msgs if q in (m.get("body","").lower())]
    return jsonify({"messages": msgs[:max(1, min(limit, 1000))]})

@app.route("/admin/intents", methods=["GET"])
@require_token
def admin_intents():
    store = _load_store()
    counts = {}
    for m in store.get("messages", []):
        k = (m.get("intent") or "other")
        counts[k] = counts.get(k, 0) + 1
    intents = [{"intent": k, "count": v} for k,v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return jsonify({"intents": intents})

@app.route("/admin/export.csv", methods=["GET"])
@require_token
def admin_export_csv():
    store = _load_store()
    rows = store.get("messages", [])
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ts","direction","kind","from","to","body","intent","note"])
    for m in rows:
        w.writerow([m.get("ts",""), m.get("direction",""), m.get("kind",""),
                    m.get("from",""), m.get("to",""), m.get("body",""),
                    m.get("intent",""), m.get("note","")])
    data = out.getvalue()
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=export.csv"})

# -------------------------
# Error handling
# -------------------------
@app.errorhandler(400)
def err_400(e): return jsonify({"ok":False,"error":"bad_request"}), 400

@app.errorhandler(401)
def err_401(e): return jsonify({"ok":False,"error":"unauthorized"}), 401

@app.errorhandler(404)
def err_404(e): return jsonify({"ok":False,"error":"not_found"}), 404

@app.errorhandler(Exception)
def err_500(e):
    # never leak stack traces—just a stable message
    return jsonify({"ok":False,"error":"internal_server_error"}), 500

# -------------------------
# Local run
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)