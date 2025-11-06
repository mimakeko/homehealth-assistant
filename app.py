# app.py
# Home Health Assistant – unified Flask app
# Includes: health/metrics/admin/simulate/send/schedule UI + Google test endpoints

import os, json, time, csv, io
from datetime import datetime
from urllib.parse import urlencode
from collections import Counter

from flask import Flask, request, jsonify, make_response, abort

# requests is only used for Google APIs; import gently
try:
    import requests
except Exception:
    requests = None

app = Flask(__name__)

# ---------------------------------------------------------
# Env / Config
# ---------------------------------------------------------
SERVICE_NAME = "Home Health Assistant"
BUILD_ID = os.getenv("HEROKU_SLUG_COMMIT") or os.getenv("RENDER_GIT_COMMIT") or "local"
REGION = os.getenv("RENDER_REGION", "local")
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")

DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "").strip()
DEBUG_USER = os.getenv("DEBUG_USER", "admin").strip()

# Twilio envs are optional at runtime
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
TWILIO_READY = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID])

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
MAPS_READY = bool(GOOGLE_MAPS_API_KEY and requests)

STORE_BACKEND = os.getenv("STORE_BACKEND", "json").strip().lower()  # json or sqlite (json default)
ADMIN_PAGE_SIZE = int(os.getenv("ADMIN_PAGE_SIZE", "50"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_MAX_SIMULATE = int(os.getenv("RATE_LIMIT_MAX_SIMULATE", "30"))

# ---------------------------------------------------------
# Runtime state / store (simple JSON file; safe for Render)
# ---------------------------------------------------------
DATA_DIR = os.path.abspath("./data")
os.makedirs(DATA_DIR, exist_ok=True)
STORE_PATH = os.path.join(DATA_DIR, "store.json")

def _load_store():
    if not os.path.exists(STORE_PATH):
        return {"messages": []}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"messages": []}

def _save_store(store):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False)
    os.replace(tmp, STORE_PATH)

def add_message(direction, kind, body, to="", note=""):
    store = _load_store()
    store.setdefault("messages", [])
    store["messages"].append({
        "direction": direction,          # 'in' or 'out'
        "kind": kind,                    # 'simulate' or 'twilio'
        "body": body,
        "to": to,
        "note": note,
        "ts": time.time()
    })
    _save_store(store)

def list_messages(limit=50, text_filter=None):
    store = _load_store()
    msgs = list(reversed(store.get("messages", [])))
    if text_filter:
        t = text_filter.lower()
        msgs = [m for m in msgs if t in (m.get("body","").lower())]
    return msgs[:max(1, min(1000, limit))]

# ---------------------------------------------------------
# Auth helpers (same as before): header, query, or cookie
# ---------------------------------------------------------
def get_token_from_request():
    # 1) Header
    tok = request.headers.get("X-Debug-Token", "").strip()
    if tok:
        return tok
    # 2) Query
    tok = request.args.get("token", "").strip()
    if tok:
        return tok
    # 3) Cookie
    tok = request.cookies.get("access_token", "").strip()
    return tok

def require_token():
    tok = get_token_from_request()
    if not DEBUG_TOKEN or tok != DEBUG_TOKEN:
        abort(401)

# ---------------------------------------------------------
# Health / Metrics / Debug
# ---------------------------------------------------------
start_time = time.time()

def uptime_seconds():
    return round(time.time() - start_time, 2)

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": SERVICE_NAME, "status": "ok", "mode": "mock"})

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "mode": "mock" if not TWILIO_READY else "live",
        "service": SERVICE_NAME,
        "status": "ok",
        "twilio_ready": TWILIO_READY,
        "maps_ready": MAPS_READY,
        "uptime_seconds": uptime_seconds()
    })

# basic counters
COUNTERS = {
    "requests": 0,
    "errors": 0,
    "avg_latency_seconds": 0.0,
}
def _observe(latency):
    COUNTERS["requests"] += 1
    # simple moving average
    r = COUNTERS["requests"]
    COUNTERS["avg_latency_seconds"] = (
        ((r - 1) * COUNTERS["avg_latency_seconds"] + latency) / r
    )

@app.before_request
def _start_timer():
    request._t0 = time.time()

@app.after_request
def _stop_timer(resp):
    try:
        dt = max(0.0, time.time() - getattr(request, "_t0", time.time()))
        _observe(dt)
    except Exception:
        pass
    return resp

@app.errorhandler(500)
def _server_error(e):
    COUNTERS["errors"] += 1
    return make_response("<h1>Internal Server Error</h1>", 500)

@app.route("/metrics", methods=["GET"])
def metrics_json():
    return jsonify({
        "status": "ok",
        "service": SERVICE_NAME,
        "mode": "mock" if not TWILIO_READY else "live",
        "uptime_seconds": uptime_seconds(),
        "healthz": {
            "requests": COUNTERS["requests"],
            "errors": COUNTERS["errors"],
            "avg_latency_seconds": round(COUNTERS["avg_latency_seconds"], 5),
        },
        "sms": {"twilio_ready": TWILIO_READY},
        "build_id": BUILD_ID,
        "region": REGION,
        "git_commit": os.getenv("RENDER_GIT_COMMIT", BUILD_ID),
        "version": APP_VERSION,
    })

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    # minimal Prometheus exposition
    lines = [
        "# HELP hha_requests_total Total requests",
        "# TYPE hha_requests_total counter",
        f"hha_requests_total {COUNTERS['requests']}",
        "# HELP hha_errors_total Total errors",
        "# TYPE hha_errors_total counter",
        f"hha_errors_total {COUNTERS['errors']}",
        "# HELP hha_latency_seconds_avg Average latency (s)",
        "# TYPE hha_latency_seconds_avg gauge",
        f"hha_latency_seconds_avg {COUNTERS['avg_latency_seconds']}",
        "# HELP hha_uptime_seconds Uptime (s)",
        "# TYPE hha_uptime_seconds gauge",
        f"hha_uptime_seconds {uptime_seconds()}",
    ]
    return ("\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; charset=utf-8"})

# Simple debug page (token required)
@app.route("/debug", methods=["GET"])
def debug_page():
    tok = get_token_from_request()
    if not DEBUG_TOKEN or tok != DEBUG_TOKEN:
        # tiny HTML prompt
        html = f"""<!doctype html><meta charset="utf-8">
        <h1>Enter Access Token</h1>
        <form method="get">
          <input name="token" placeholder="DEBUG_TOKEN" size="40">
          <button>Access</button>
        </form>"""
        return (html, 401)
    # show dashboard
    html = f"""<!doctype html><meta charset="utf-8">
    <title>Home Health Assistant Debug Dashboard</title>
    <h1>Home Health Assistant Debug Dashboard</h1>
    <p>Status: <b>ok</b></p>
    <table border="1" cellpadding="6">
      <tr><td>MODE</td><td>{"mock" if not TWILIO_READY else "live"}</td></tr>
      <tr><td>UPTIME (SEC)</td><td>{uptime_seconds():.2f}</td></tr>
      <tr><td>TWILIO READY</td><td>{TWILIO_READY}</td></tr>
      <tr><td>MAPS READY</td><td>{MAPS_READY}</td></tr>
      <tr><td>REGION</td><td>{REGION}</td></tr>
      <tr><td>BUILD ID</td><td>{BUILD_ID}</td></tr>
      <tr><td>GIT COMMIT</td><td>{os.getenv("RENDER_GIT_COMMIT", BUILD_ID)}</td></tr>
      <tr><td>VERSION</td><td>{APP_VERSION}</td></tr>
      <tr><td>BUILD TIME</td><td>{datetime.utcnow().isoformat()}+00:00</td></tr>
    </table>
    <p>Tip: You can also use a query token if configured: <code>/debug?token=YOUR_TOKEN</code></p>
    """
    resp = make_response(html)
    # set cookie for 5 days (UI uses this)
    resp.set_cookie("access_token", DEBUG_TOKEN, max_age=5 * 24 * 3600, httponly=False, samesite="Lax")
    return resp

# ---------------------------------------------------------
# Admin endpoints (token protected)
# ---------------------------------------------------------
@app.route("/admin/messages", methods=["GET"])
def admin_messages():
    require_token()
    limit = int(request.args.get("limit", ADMIN_PAGE_SIZE))
    text = request.args.get("search", "").strip() or None
    return jsonify(list_messages(limit=limit, text_filter=text))

@app.route("/admin/intents", methods=["GET"])
def admin_intents():
    require_token()
    msgs = list_messages(limit=1000)
    intents = Counter()
    for m in msgs:
        body = m.get("body", "").lower()
        if "confirm" in body:
            intents["confirm"] += 1
        else:
            intents["other"] += 1
    return jsonify({"intents": [{"intent": k, "count": v} for k, v in intents.items()]})

@app.route("/admin/export.csv", methods=["GET"])
def admin_export_csv():
    require_token()
    msgs = list_messages(limit=1000)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["direction", "kind", "body", "to", "note", "ts"])
    for m in msgs:
        w.writerow([m.get("direction",""), m.get("kind",""), m.get("body",""), m.get("to",""), m.get("note",""), m.get("ts","")])
    resp = make_response(out.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = 'attachment; filename="export.csv"'
    return resp

# simple HTML admin UI (token box)
ADMIN_HTML = """<!doctype html><meta charset="utf-8"><title>Home Health Assistant — Admin</title>
<h1>Home Health Assistant — Admin</h1>
<label>Token <input id="tok" size="40" placeholder="DEBUG_TOKEN"></label>
<label>Search <input id="q" placeholder="text filter"></label>
<label>Limit <input id="lim" type="number" value="50"></label>
<button onclick="load()">Load</button>
<button onclick="csv()">Export CSV</button>
<div id="n"></div>
<table border="1" cellpadding="6" id="t"></table>
<script>
async function load(){
  const tok = document.getElementById('tok').value.trim();
  const q = document.getElementById('q').value.trim();
  const lim = document.getElementById('lim').value || '50';
  const r = await fetch(`/admin/messages?limit=${lim}&search=${encodeURIComponent(q)}`, {headers:{'X-Debug-Token':tok}});
  if(!r.ok){ alert('Auth or fetch error'); return; }
  const data = await r.json();
  document.getElementById('n').textContent = `${data.length} messages`;
  const rows = ['<tr><th>body</th><th>direction</th><th>kind</th><th>note</th><th>to</th><th>ts</th></tr>']
  data.forEach(m=>{
    rows.push(`<tr><td>${m.body||''}</td><td>${m.direction||''}</td><td>${m.kind||''}</td><td>${m.note||''}</td><td>${m.to||''}</td><td>${m.ts||''}</td></tr>`)
  });
  document.getElementById('t').innerHTML = rows.join('');
}
function csv(){
  const tok = document.getElementById('tok').value.trim();
  const u = `/admin/export.csv`;
  fetch(u, {headers:{'X-Debug-Token':tok}}).then(async r=>{
    if(!r.ok){ alert('Auth error'); return; }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'export.csv';
    a.click();
  });
}
</script>
"""
@app.route("/admin", methods=["GET"])
def admin_ui():
    return ADMIN_HTML

# ---------------------------------------------------------
# SMS simulate / send (send is mock while A2P not ready)
# ---------------------------------------------------------
@app.route("/simulate-sms", methods=["POST"])
def simulate_sms():
    data = request.get_json(force=True, silent=True) or {}
    body = (data.get("body") or "").strip()
    add_message("in", "simulate", body, to="", note="")
    ok = True
    intent = "confirm" if "confirm" in body.lower() else "other"
    return jsonify({"ok": ok, "intent": intent, "echo": body})

@app.route("/send-sms", methods=["POST"])
def send_sms():
    data = request.get_json(force=True, silent=True) or {}
    to = (data.get("to") or "").strip()
    body = (data.get("body") or "").strip()
    # For now, mock send regardless of TWILIO_READY to keep it safe.
    add_message("out", "simulate", body, to=to, note="auto-confirm")
    return jsonify({"status": "mock-sent", "sid": f"mock-{int(time.time()*1000)}"})

# ---------------------------------------------------------
# Simple scheduling UI + endpoints
# ---------------------------------------------------------
UI_SCHEDULE = """<!doctype html><meta charset="utf-8"><title>Schedule</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:15px system-ui;margin:16px}
h1{margin:0 0 10px}
controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,button,select{padding:8px;font:inherit}
card{border:1px solid #ddd;border-radius:8px;padding:10px;margin:8px 0}
row{display:flex;justify-content:space-between;gap:8px}
.badge{background:#eef;padding:2px 6px;border-radius:6px}
</style>
<h1>Day Schedule</h1>
<div class="controls">
  <label>Date <input id="d" type="date"></label>
  <label>Therapist <input id="t" placeholder="(optional)"></label>
  <button onclick="load()">Load</button>
  <button onclick="opt()">Optimize</button>
  <small id="tokMsg"></small>
</div>
<div id="list"></div>
<script>
(function init(){
  const today = new Date().toISOString().slice(0,10);
  document.getElementById('d').value = today;
  const tok = new URLSearchParams(location.search).get('token') || '';
  if(tok){
    document.cookie = 'access_token='+tok+'; max-age='+(5*24*3600)+'; path=/; samesite=Lax';
    document.getElementById('tokMsg').textContent = 'Token set in cookie for 5d.';
  }
})();
function fmt(ts){ return new Date(ts*1000).toLocaleString(); }

async function load(){
  const date = document.getElementById('d').value || new Date().toISOString().slice(0,10);
  const therapist = document.getElementById('t').value.trim();
  const r = await fetch(`/schedule?date=${encodeURIComponent(date)}${therapist?`&therapist=${encodeURIComponent(therapist)}`:''}`);
  if(!r.ok){ alert('Load error: HTTP '+r.status+' '+(await r.text())); return; }
  const j = await r.json();
  const L = document.getElementById('list'); L.innerHTML = '';
  (j.appointments||[]).forEach(a=>{
    const div = document.createElement('card');
    div.innerHTML = `<b>${a.patient_name||'(unknown)'}</b> <span class="badge">${a.status||''}</span>
      <div class="small">${a.start_iso||''}</div>
      <div class="small">${a.address||''}, ${a.city||''}, ${a.state||''} ${a.zip||''}</div>
      <div class="small">${a.lat!=null && a.lon!=null ? ('Lat,Lon: '+a.lat+', '+a.lon) : ''}</div>
      <div class="small">phone: ${a.patient_phone||''}</div>`;
    L.appendChild(div);
  });
}
async function opt(){
  const date = document.getElementById('d').value || new Date().toISOString().slice(0,10);
  const therapist = document.getElementById('t').value.trim();
  const r = await fetch('/schedule/optimize',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({date,therapist})});
  if(!r.ok){ alert('Optimize error: '+(await r.text())); return; }
  const j = await r.json();
  alert('Optimized. DriveTime: '+(j.drive_time?'ON':'OFF'));
}
</script>
"""

@app.route("/ui/schedule", methods=["GET"])
def ui_schedule():
    # Require token (header/query/cookie). If missing show password box.
    tok = get_token_from_request()
    if not DEBUG_TOKEN or tok != DEBUG_TOKEN:
        html = f"""<!doctype html><meta charset="utf-8">
        <h1>Enter Access Token</h1>
        <form method="get">
          <input name="token" placeholder="DEBUG_TOKEN" size="40">
          <button>Access</button>
        </form>"""
        return (html, 401)
    # set cookie so user doesn’t have to paste token again
    resp = make_response(UI_SCHEDULE)
    resp.set_cookie("access_token", DEBUG_TOKEN, max_age=5 * 24 * 3600, httponly=False, samesite="Lax")
    return resp

@app.route("/schedule", methods=["GET"])
def schedule_get():
    # Require cookie/header token (read-only data too)
    require_token()
    date = (request.args.get("date") or datetime.utcnow().date().isoformat()).strip()
    therapist = (request.args.get("therapist") or "").strip()

    # Demo stub data
    appts = [{
        "patient_name": "John Doe",
        "start_iso": f"{date}T09:30:00",
        "status": "Scheduled",
        "address": "1 Apple Park Way",
        "city": "Cupertino",
        "state": "CA",
        "zip": "95014",
        "patient_phone": "+14085550100",
    },{
        "patient_name": "Jane Smith",
        "start_iso": f"{date}T11:00:00",
        "status": "Scheduled",
        "address": "1600 Amphitheatre Parkway",
        "city": "Mountain View",
        "state": "CA",
        "zip": "94043",
        "patient_phone": "+14085550101",
    }]

    # If Maps is ready, geocode addresses to lat/lon (best-effort)
    if MAPS_READY:
        for a in appts:
            addr = f"{a['address']}, {a['city']}, {a['state']} {a['zip']}"
            try:
                url = "https://maps.googleapis.com/maps/api/geocode/json?" + urlencode({"address": addr, "key": GOOGLE_MAPS_API_KEY})
                r = requests.get(url, timeout=10)
                g = r.json()
                if g.get("results"):
                    loc = g["results"][0]["geometry"]["location"]
                    a["lat"] = loc["lat"]; a["lon"] = loc["lng"]
            except Exception:
                pass

    return jsonify({"appointments": appts, "status": "ok"})

@app.route("/schedule/optimize", methods=["POST"])
def schedule_optimize():
    require_token()
    data = request.get_json(force=True, silent=True) or {}
    date = (data.get("date") or datetime.utcnow().date().isoformat()).strip()
    therapist = (data.get("therapist") or "").strip()

    # Minimal demo result. When Maps is ready, we pretend we computed drive time.
    result = {
        "date": date,
        "therapist": therapist or "unknown",
        "drive_time": bool(MAPS_READY),
        "ok": True,
    }
    return jsonify(result)

# ---------------------------------------------------------
# Google Maps test endpoints (NEW)
# ---------------------------------------------------------
@app.route("/test/geocode", methods=["GET"])
def test_geocode():
    require_token()
    if not MAPS_READY:
        return jsonify({"error": "Google Maps not configured (missing key or requests)"}), 400

    address = request.args.get("address", "").strip()
    if not address:
        return jsonify({"error": "missing address"}), 400

    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json?" + urlencode({"address": address, "key": GOOGLE_MAPS_API_KEY})
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            return jsonify({"lat": loc["lat"], "lon": loc["lng"], "status": "ok"})
        return jsonify({"status": "no_results", "raw": data}), 404
    except Exception as e:
        return jsonify({"error": "request_failed", "detail": str(e)}), 502

@app.route("/test/distance", methods=["GET"])
def test_distance():
    require_token()
    if not MAPS_READY:
        return jsonify({"error": "Google Maps not configured (missing key or requests)"}), 400

    origin = request.args.get("from", "").strip()
    destination = request.args.get("to", "").strip()
    if not origin or not destination:
        return jsonify({"error": "missing parameters 'from' and/or 'to'"}), 400

    try:
        url = "https://maps.googleapis.com/maps/api/distancematrix/json?" + urlencode({
            "origins": origin,
            "destinations": destination,
            "key": GOOGLE_MAPS_API_KEY
        })
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("rows") and data["rows"][0]["elements"]:
            elem = data["rows"][0]["elements"][0]
            if elem.get("status") == "OK":
                return jsonify({
                    "distance_km": round(elem["distance"]["value"] / 1000, 3),
                    "duration_min": round(elem["duration"]["value"] / 60, 1),
                    "status": "ok"
                })
        return jsonify({"status": "no_results", "raw": data}), 404
    except Exception as e:
        return jsonify({"error": "request_failed", "detail": str(e)}), 502

# ---------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)