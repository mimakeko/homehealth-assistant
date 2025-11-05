import os, json, time, hashlib, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response, abort, make_response

# --- Optional Twilio import (mock if not installed or creds missing)
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

app = Flask(__name__)

# -------------------------
# Config from environment
# -------------------------
DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "").strip()
FLASK_ENV = os.getenv("FLASK_ENV", "production")
PYTHON_VERSION = os.getenv("PYTHON_VERSION", "")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()

STORE_BACKEND = os.getenv("STORE_BACKEND", "json").strip().lower()  # 'json' (default)
ADMIN_PAGE_SIZE = int(os.getenv("ADMIN_PAGE_SIZE", "50"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))  # 1 minute
RATE_LIMIT_MAX_SIMULATE = int(os.getenv("RATE_LIMIT_MAX_SIMULATE", "30"))

BUILD_ID = os.getenv("RENDER_GIT_COMMIT", "") or os.getenv("SOURCE_VERSION", "")
GIT_COMMIT = (BUILD_ID or "")[:7]
BUILD_TIME = datetime.now(timezone.utc).isoformat()

START_TS = time.time()

# -------------------------
# Lightweight store
# -------------------------
_store_lock = threading.Lock()
_store_path = "store.json"
_state = {
    "messages": [],  # each: {ts, direction, kind, body, to, note, intent}
    "intent_counts": {}  # intent -> count
}

def _store_load():
    if STORE_BACKEND != "json":
        return
    if not os.path.exists(_store_path):
        return
    try:
        with open(_store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _state.update({k: data.get(k, _state[k]) for k in _state})
    except Exception:
        pass

def _store_save():
    if STORE_BACKEND != "json":
        return
    tmp = _state.copy()
    try:
        with open(_store_path, "w", encoding="utf-8") as f:
            json.dump(tmp, f, ensure_ascii=False)
    except Exception:
        pass

_store_load()

# -------------------------
# Twilio wiring (mock ok)
# -------------------------
TWILIO_READY = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_MESSAGING_SERVICE_SID and TwilioClient)
_twilio_client = None
if TWILIO_READY:
    try:
        _twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        TWILIO_READY = False
        _twilio_client = None

# -------------------------
# Helpers
# -------------------------
def uptime_seconds() -> float:
    return round(time.time() - START_TS, 2)

def _now_ts():
    return time.time()

def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]

def _record_message(direction, kind, body, to=None, note=None, intent=None):
    with _store_lock:
        rec = {
            "ts": _now_ts(),
            "direction": direction,  # 'in' or 'out'
            "kind": kind,            # 'simulate' or 'twilio'
            "body": body,
            "to": to,
            "note": note,
            "intent": intent
        }
        _state["messages"].append(rec)
        if intent:
            _state["intent_counts"][intent] = _state["intent_counts"].get(intent, 0) + 1
        # cap list to something sane in free tier memory
        if len(_state["messages"]) > 2000:
            _state["messages"] = _state["messages"][-2000:]
        _store_save()

def _parse_intent(body: str) -> str:
    b = (body or "").strip().lower()
    # extremely simple parser for demo
    keywords_confirm = ("yes", "yep", "confirm", "ok", "okay", "si", "y", "sure")
    for k in keywords_confirm:
        if k in b:
            return "confirm"
    return "other"

# -------------------------
# Rate limiter (simulate)
# -------------------------
_rl = {"window_start": int(START_TS), "count": 0}
def _rate_limit_simulate() -> bool:
    now = int(time.time())
    win = _rl["window_start"]
    if now - win >= RATE_LIMIT_WINDOW_SEC:
        _rl["window_start"] = now
        _rl["count"] = 0
    if _rl["count"] >= RATE_LIMIT_MAX_SIMULATE:
        return False
    _rl["count"] += 1
    return True

# -------------------------
# Token guard – protect JSON APIs, not the UI HTML
# -------------------------
def _token_ok():
    if not DEBUG_TOKEN:
        return True  # if not configured, allow
    tok = request.headers.get("X-Debug-Token") or request.args.get("token") or ""
    return tok == DEBUG_TOKEN

PROTECTED_PREFIXES = ("/schedule", "/optimize", "/admin", "/debug")  # UI HTML stays open

@app.before_request
def _guard_routes():
    p = request.path.rstrip("/") or "/"
    # let health/metrics/root/ui be public
    if p.startswith(PROTECTED_PREFIXES):
        if not _token_ok():
            return make_response(("Unauthorized", 401))

# -------------------------
# Basic routes
# -------------------------
@app.route("/", methods=["GET"])
def index():
    return "Home Health Assistant API (Cloud) ✅"

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "mode": "mock" if not TWILIO_READY else "twilio",
        "service": "Home Health Assistant",
        "status": "ok",
        "twilio_ready": TWILIO_READY,
        "uptime_seconds": uptime_seconds()
    })

# Simple metrics (JSON)
_METRICS_ACCUM = {
    "requests": 0,
    "errors": 0,
    "avg_latency_seconds": 0.0
}
@app.before_request
def _metrics_tick_start():
    request._t0 = time.time()

@app.after_request
def _metrics_tick_end(resp):
    try:
        dt = max(0.0, time.time() - getattr(request, "_t0", time.time()))
        _METRICS_ACCUM["requests"] += 1
        # running avg
        n = _METRICS_ACCUM["requests"]
        _METRICS_ACCUM["avg_latency_seconds"] = (
            (_METRICS_ACCUM["avg_latency_seconds"] * (n - 1) + dt) / n
        )
        if resp.status_code >= 400:
            _METRICS_ACCUM["errors"] += 1
    except Exception:
        pass
    return resp

@app.route("/metrics", methods=["GET"])
def metrics_json():
    return jsonify({
        "status": "ok",
        "service": "Home Health Assistant",
        "mode": "mock" if not TWILIO_READY else "twilio",
        "uptime_seconds": uptime_seconds(),
        "routes": {
            "/": {},
            "/healthz": {},
            "/metrics": {},
            "/metrics.prom": {},
            "/simulate-sms": {},
            "/send-sms": {},
            "/schedule": {},
            "/optimize": {}
        },
        "healthz": _METRICS_ACCUM,
        "sms": {"twilio_ready": TWILIO_READY},
        "build": {
            "build_id": BUILD_ID or "local",
            "region": os.getenv("RENDER", "local"),
            "git_commit": GIT_COMMIT or "local",
            "version": "1.1.0"
        }
    })

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    """Prometheus-style minimal metrics."""
    lines = []
    lines.append(f'hha_uptime_seconds {uptime_seconds():.2f}')
    lines.append(f'hha_requests_total {_METRICS_ACCUM["requests"]}')
    lines.append(f'hha_errors_total {_METRICS_ACCUM["errors"]}')
    lines.append(f'hha_avg_latency_seconds {_METRICS_ACCUM["avg_latency_seconds"]:.6f}')
    return Response("\n".join(lines) + "\n", mimetype="text/plain")

@app.route("/debug", methods=["GET"])
def debug_dashboard():
    # tiny text dashboard (token required via guard)
    return Response(f"""
<html><head><title>Home Health Assistant — Debug</title></head>
<body style="font:15px system-ui;margin:16px">
<h1>Home Health Assistant Debug Dashboard</h1>
<table border="1" cellspacing="0" cellpadding="6">
<tr><td>MODE</td><td>{"mock" if not TWILIO_READY else "twilio"}</td></tr>
<tr><td>UPTIME (SEC)</td><td>{uptime_seconds()}</td></tr>
<tr><td>TWILIO READY</td><td>{TWILIO_READY}</td></tr>
<tr><td>REGION</td><td>{os.getenv("RENDER", "local")}</td></tr>
<tr><td>BUILD ID</td><td>{BUILD_ID or "local"}</td></tr>
<tr><td>GIT COMMIT</td><td>{GIT_COMMIT or "local"}</td></tr>
<tr><td>VERSION</td><td>1.1.0</td></tr>
<tr><td>BUILD TIME</td><td>{BUILD_TIME}</td></tr>
</table>
<p class="small">Tip: append <code>?token=YOUR_TOKEN</code> to API calls or send it as header <code>X-Debug-Token</code>.</p>
</body></html>
""", mimetype="text/html")

# -------------------------
# SMS endpoints (mock ok)
# -------------------------
@app.route("/simulate-sms", methods=["POST"])
def simulate_sms():
    # quick rate limit to avoid hammering free dyno
    if not _rate_limit_simulate():
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    data = request.get_json(silent=True) or {}
    body = data.get("body", "")
    frm = data.get("from", "")

    intent = _parse_intent(body)
    _record_message("in", "simulate", body, to=frm, intent=intent)

    return jsonify({"echo": body, "intent": intent, "ok": True})

@app.route("/send-sms", methods=["POST"])
def send_sms():
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or "").strip()
    body = (data.get("body") or "").strip()

    if not to or not body:
        return jsonify({"ok": False, "error": "missing to/body"}), 400

    if TWILIO_READY and _twilio_client:
        try:
            msg = _twilio_client.messages.create(
                to=to,
                messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
                body=body
            )
            sid = msg.sid
            _record_message("out", "twilio", body, to=to, note="sent", intent=None)
            return jsonify({"sid": sid, "status": "twilio-sent"})
        except Exception as e:
            _record_message("out", "twilio", body, to=to, note=f"twilio-error:{e}", intent=None)
            return jsonify({"ok": False, "error": "twilio_error"}), 500
    else:
        # mock
        sid = "mock-" + str(int(time.time()))
        _record_message("out", "simulate", body, to=to, note="auto-confirm", intent=None)
        return jsonify({"sid": sid, "status": "mock-sent"})

# -------------------------
# Admin (token-protected by guard)
# -------------------------
@app.route("/admin/messages", methods=["GET"])
def admin_messages():
    limit = int(request.args.get("limit", ADMIN_PAGE_SIZE))
    search = (request.args.get("search") or "").strip().lower()
    with _store_lock:
        items = list(_state["messages"])
    if search:
        items = [m for m in items if search in (m.get("body","")+m.get("note","")).lower()]
    items = items[-limit:]
    return jsonify({"messages": items, "intent_counts": _state["intent_counts"]})

@app.route("/admin/export.csv", methods=["GET"])
def admin_export_csv():
    # very small CSV export
    with _store_lock:
        items = list(_state["messages"])
    header = "ts,direction,kind,body,to,note,intent\n"
    rows = []
    for m in items:
        row = [
            f'{m.get("ts",0)}',
            m.get("direction",""),
            m.get("kind",""),
            (m.get("body","").replace('"','""')),
            m.get("to",""),
            (m.get("note","") or "").replace('"','""'),
            (m.get("intent","") or "")
        ]
        rows.append(",".join(f'"{c}"' for c in row))
    csv = header + "\n".join(rows)
    return Response(csv, mimetype="text/csv")

# -------------------------
# Scheduling demo (token-protected JSON)
# -------------------------
def _fake_appts(date_str, therapist=None):
    # minimal fake list to demo UI
    base = int(hash(date_str) & 0xffff) % 50
    pts = []
    for i in range(5):
        ts = datetime.fromisoformat(date_str + "T0{}:00:00".format(9+i))
        pts.append({
            "patient_name": f"Patient {base+i}",
            "status": "scheduled" if i % 2 == 0 else "pending",
            "start_iso": ts.isoformat(),
            "address1": f"{100+i} Main St",
            "city": "Allen",
            "state": "TX",
            "zip": "75002",
            "lat": 33.103 + i*0.01,
            "lon": -96.673 - i*0.01,
            "patient_phone": "+197255501{:02d}".format(i),
            "duration_min": 60,
            "therapist": therapist or "Therapist A"
        })
    return pts

@app.route("/schedule", methods=["GET"])
def schedule_get():
    date_str = (request.args.get("date") or datetime.now().date().isoformat())[:10]
    therapist = request.args.get("therapist")
    appts = _fake_appts(date_str, therapist)
    return jsonify({"appointments": appts})

@app.route("/optimize", methods=["POST"])
def schedule_optimize():
    data = request.get_json(silent=True) or {}
    date_str = (data.get("date") or datetime.now().date().isoformat())[:10]
    therapist = data.get("therapist") or ""
    # demo: pretend we optimized drive sequence; flag returns True
    return jsonify({"ok": True, "date": date_str, "therapist": therapist, "drivetime": True})

# -------------------------
# Public UI (NO token)
# -------------------------
UI_SCHEDULE = """
<!doctype html>
<meta charset="utf-8"><title>Schedule</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:15px system-ui;margin:16px}
h1{margin:0 0 10px}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,button,select{padding:8px;min-height:36px}
.card{border:1px solid #ddd;border-radius:8px;padding:10px;margin:8px 0}
.row{display:flex;justify-content:space-between;gap:8px}
.badge{background:#eef;padding:2px 6px;border-radius:6px}
.small{color:#666;font-size:13px}
#tokwrap{display:none;margin-top:12px}
</style>

<h1>Day Schedule</h1>

<div class="controls">
  <label>Date <input id="d" type="date"></label>
  <label>Therapist <input id="t" placeholder="(optional)"></label>
  <button onclick="load()">Load</button>
  <button onclick="opt()">Optimize</button>
</div>

<div id="tokwrap" class="card">
  <b>Enter Access Token</b><br>
  <input id="tok" placeholder="DEBUG_TOKEN" size="40">
  <button onclick="saveTok()">Access</button>
</div>

<div id="list"></div>

<script>
const fmt = ts => new Date(ts*1000).toLocaleString();

function getUrlTok(){ const u = new URL(location.href); return u.searchParams.get("token")||""; }
function getSavedTok(){ try{return localStorage.getItem("hha_token")||"";}catch(e){return "";} }
function setSavedTok(v){ try{localStorage.setItem("hha_token", v||"");}catch(e){} }

function currentTok(){
  const q = getUrlTok(); if(q){ setSavedTok(q); return q; }
  const s = getSavedTok(); if(s) return s;
  const el = document.getElementById("tok"); return el ? (el.value||"").trim() : "";
}
function ensureTokUI(){
  const tok = currentTok(), tw = document.getElementById("tokwrap"), ti = document.getElementById("tok");
  if (tok){ if(ti) ti.value = tok; if(tw) tw.style.display="none"; return tok; }
  if (tw) tw.style.display="block"; return "";
}
function saveTok(){
  const v = (document.getElementById("tok").value||"").trim();
  if(!v){alert("Please paste your access token.");return;}
  setSavedTok(v);
  const u = new URL(location.href); u.searchParams.set("token", v); location.href = u.toString();
}
async function api(path, opts){
  const tok = currentTok(); if(!tok){ ensureTokUI(); throw new Error("missing token"); }
  opts = opts||{}; opts.headers = Object.assign({}, opts.headers||{}, {"X-Debug-Token": tok});
  const r = await fetch(path, opts);
  if(r.status===401){ ensureTokUI(); throw new Error("unauthorized"); }
  if(!r.ok){ const t = await r.text(); throw new Error("HTTP "+r.status+": "+t); }
  return r.json();
}
async function load(){
  const d = document.getElementById('d').value || new Date().toISOString().slice(0,10);
  const t = document.getElementById('t').value.trim();
  if(!ensureTokUI()) return;
  try{
    const r = await api(`/schedule?date=${encodeURIComponent(d)}${t?`&therapist=${encodeURIComponent(t)}`:''}`);
    const list = document.getElementById('list'); list.innerHTML="";
    if(!r.appointments||!r.appointments.length){ list.textContent="No appointments."; return; }
    r.appointments.forEach(a=>{
      const div=document.createElement('div'); div.className='card';
      div.innerHTML = `
        <div class="row"><b>${a.patient_name||'unknown'}</b> <span class="badge">${a.status||''}</span></div>
        <div class="small">${a.start_iso||''}</div>
        <div class="small">${[a.address1||'',a.city||'',a.state||'',a.zip||''].filter(Boolean).join(" ")}</div>
        <div class="small">${(a.lat!=null && a.lon!=null)?('Lat,Lon: '+a.lat+', '+a.lon):''}</div>
        <div class="small">${a.therapist||''} • Duration: ${a.duration_min||60} min</div>
        <div class="small">${a.patient_phone?(`<a target="_blank" href="tel:${a.patient_phone}">Call</a>`):''}
          ${a.lat&&a.lon?` • <a target="_blank" href="https://maps.google.com/?q=${a.lat},${a.lon}">Map</a>`:''}</div>`;
      list.appendChild(div);
    });
  }catch(e){ alert("Load error: "+e.message); }
}
async function opt(){
  const d = document.getElementById('d').value || new Date().toISOString().slice(0,10);
  const t = document.getElementById('t').value.trim();
  if(!ensureTokUI()) return;
  try{
    const r = await api("/optimize",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({date:d,therapist:t||null})});
    alert(`Optimized. DriveTime: ${r.drivetime?"ON":"OFF"}`);
  }catch(e){ alert("Optimize error: "+e.message); }
}
(function init(){ const d=document.getElementById('d'); d.value=new Date().toISOString().slice(0,10); ensureTokUI(); })();
</script>
"""

@app.route("/ui/schedule", methods=["GET"])
def ui_schedule():
    return Response(UI_SCHEDULE, mimetype="text/html")

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    # Local run; on Render, Gunicorn runs app:app
    app.run(host="0.0.0.0", port=5000, debug=(FLASK_ENV == "development"))