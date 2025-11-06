import os, time, json, csv, io, math
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlencode

import requests
from flask import (
    Flask, request, jsonify, abort, make_response,
    Response
)

# -----------------------------
# App
# -----------------------------
app = Flask(__name__)

START_TS = time.time()

# -----------------------------
# Env / Config
# -----------------------------
DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "").strip()
DEBUG_USER  = os.getenv("DEBUG_USER",  "").strip()

TWILIO_ACCOUNT_SID        = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN         = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE  = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
TWILIO_READY              = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE])

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
MAPS_READY = bool(GOOGLE_MAPS_API_KEY)

STORE_BACKEND = os.getenv("STORE_BACKEND", "json").strip().lower()   # "json" (default) or "sqlite" (future)
STORE_PATH    = os.getenv("STORE_PATH", "/tmp/hha_store.json").strip()

ADMIN_PAGE_SIZE        = int(os.getenv("ADMIN_PAGE_SIZE", "50"))
RATE_LIMIT_WINDOW_SEC  = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_MAX_SIM     = int(os.getenv("RATE_LIMIT_MAX_SIMULATE", "30"))

# -----------------------------
# Simple JSON Store
# -----------------------------
def _load_store() -> Dict[str, Any]:
    if not os.path.exists(STORE_PATH):
        return {"messages": []}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"messages": []}

def _save_store(data: Dict[str, Any]) -> None:
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)

def _append_message(msg: Dict[str, Any]) -> None:
    data = _load_store()
    data.setdefault("messages", []).append(msg)
    _save_store(data)

# -----------------------------
# Auth helper (header, query, or cookie)
# -----------------------------
def _get_token_from_request() -> str:
    # priority: header -> query -> cookie
    token = request.headers.get("X-Debug-Token", "").strip()
    if not token:
        token = request.args.get("token", "").strip()
    if not token:
        token = request.cookies.get("debug_token", "").strip()
    return token

def _require_token() -> None:
    if not DEBUG_TOKEN:
        # if you haven't set a token, allow (dev mode)
        return
    token = _get_token_from_request()
    if token != DEBUG_TOKEN:
        abort(401)

# -----------------------------
# Utility
# -----------------------------
def _uptime_seconds() -> float:
    return round(time.time() - START_TS, 2)

def _json_ok(obj: Dict[str, Any]) -> Response:
    return make_response(jsonify(obj), 200)

def _json_err(msg: str, code: int = 400) -> Response:
    return make_response(jsonify({"error": "bad_request", "message": msg}), code)

# -----------------------------
# Google Maps helpers
# -----------------------------
MAPS_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
MAPS_DISTANCE_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

def geocode_address(addr: str) -> Optional[Tuple[float, float, str]]:
    """Return (lat, lon, formatted_address) or None."""
    if not MAPS_READY:
        return None
    try:
        r = requests.get(MAPS_GEOCODE_URL, params={"address": addr, "key": GOOGLE_MAPS_API_KEY}, timeout=10)
        j = r.json()
        if j.get("status") != "OK" or not j.get("results"):
            return None
        res = j["results"][0]
        loc = res["geometry"]["location"]
        return (loc["lat"], loc["lng"], res.get("formatted_address", addr))
    except Exception:
        return None

def distance_matrix(orig: Tuple[float, float], dest: Tuple[float, float]) -> Optional[Dict[str, Any]]:
    """Return {seconds, meters, text_distance, text_duration} or None."""
    if not MAPS_READY:
        return None
    try:
        params = {
            "origins": f"{orig[0]},{orig[1]}",
            "destinations": f"{dest[0]},{dest[1]}",
            "key": GOOGLE_MAPS_API_KEY
        }
        r = requests.get(MAPS_DISTANCE_URL, params=params, timeout=10)
        j = r.json()
        if j.get("status") != "OK":
            return None
        rows = j.get("rows", [])
        if not rows or not rows[0].get("elements"):
            return None
        el = rows[0]["elements"][0]
        if el.get("status") != "OK":
            return None
        return {
            "seconds": el["duration"]["value"],
            "meters": el["distance"]["value"],
            "text_distance": el["distance"]["text"],
            "text_duration": el["duration"]["text"],
        }
    except Exception:
        return None

# -----------------------------
# Routes
# -----------------------------
@app.route("/", methods=["GET"])
def root():
    return "Home Health Assistant API (Cloud) ✅"

@app.route("/healthz", methods=["GET"])
def healthz():
    return _json_ok({
        "mode": "mock" if not TWILIO_READY else "live",
        "service": "Home Health Assistant",
        "status": "ok",
        "twilio_ready": TWILIO_READY,
        "maps_ready": MAPS_READY,
        "uptime_seconds": _uptime_seconds(),
    })

@app.route("/version", methods=["GET"])
def version():
    build_id = os.getenv("RENDER_GIT_COMMIT", "local")
    region   = os.getenv("RENDER_REGION", "local")
    ver      = os.getenv("HHA_VERSION", "1.0.0")
    build_ts = datetime.utcfromtimestamp(START_TS).isoformat() + "Z"
    return _json_ok({
        "build_id": build_id,
        "region": region,
        "git_commit": build_id[:7] if build_id != "local" else "local",
        "service": "Home Health Assistant",
        "version": ver,
        "build_time": build_ts
    })

# --- SMS (mock until Twilio campaign live) ---
@app.route("/send-sms", methods=["POST"])
def send_sms():
    _require_token()
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or "").strip()
    body = (data.get("body") or "").strip()
    if not to or not body:
        return _json_err("Missing 'to' or 'body'")
    # record the outbound message (mock)
    _append_message({
        "ts": time.time(),
        "kind": "simulate",
        "direction": "out",
        "to": to,
        "body": body,
        "note": "auto-confirm" if "confirm" in body.lower() else "",
    })
    return _json_ok({"sid": f"mock-{int(time.time()*1000)}", "status": "mock-sent"})

@app.route("/simulate-sms", methods=["POST"])
def simulate_sms():
    """Add an inbound message (simulate patient reply)."""
    _require_token()
    data = request.get_json(silent=True) or {}
    to   = (data.get("to") or "").strip()  # their phone
    body = (data.get("body") or "").strip()
    if not to or not body:
        return _json_err("Missing 'to' or 'body'")
    _append_message({
        "ts": time.time(),
        "kind": "simulate",
        "direction": "in",
        "to": to,
        "body": body,
        "note": "",
    })
    # Very lightweight intent tagging for demo
    intent = "confirm" if "confirm" in body.lower() or "yes" in body.lower() else "other"
    return _json_ok({"echo": body, "intent": intent, "ok": True})

# --- Admin / export (token required) ---
@app.route("/admin/messages", methods=["GET"])
def admin_messages():
    _require_token()
    limit = max(1, min(1000, int(request.args.get("limit", str(ADMIN_PAGE_SIZE)))))
    q = (request.args.get("q") or "").strip().lower()
    data = _load_store()
    msgs = list(reversed(data.get("messages", [])))
    if q:
        msgs = [m for m in msgs if q in json.dumps(m).lower()]
    msgs = msgs[:limit]
    return _json_ok({"messages": msgs})

@app.route("/admin/export.csv", methods=["GET"])
def admin_export_csv():
    _require_token()
    data = _load_store().get("messages", [])
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ts", "direction", "kind", "note", "to", "body"])
    for m in data:
        writer.writerow([m.get("ts"), m.get("direction"), m.get("kind"), m.get("note"), m.get("to"), m.get("body")])
    resp = make_response(out.getvalue(), 200)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = "attachment; filename=export.csv"
    return resp

# --- Schedule API (token required) ---
@app.route("/schedule", methods=["GET"])
def schedule_get():
    _require_token()
    date = request.args.get("date", "").strip()
    therapist = (request.args.get("therapist") or "").strip()
    if not date:
        return _json_err("Missing date (YYYY-MM-DD)")
    # For now: return empty "appointments" skeleton. You can later fill from Airtable/DB.
    return _json_ok({
        "date": date,
        "therapist": therapist or None,
        "appointments": []
    })

@app.route("/schedule/optimize", methods=["POST"])
def schedule_optimize():
    _require_token()
    data = request.get_json(silent=True) or {}
    date = (data.get("date") or "").strip()
    therapist = (data.get("therapist") or "").strip()
    appts: List[Dict[str, Any]] = data.get("appointments") or []

    if not date:
        return _json_err("Missing 'date' in body")
    # If addresses are provided, compute basic pairwise drive time for successive stops.
    drive_time_total = 0
    if MAPS_READY and appts:
        # Geocode each appointment address
        coords: List[Optional[Tuple[float, float]]] = []
        for a in appts:
            addr = (a.get("address") or "").strip()
            g = geocode_address(addr) if addr else None
            if g:
                a["lat"], a["lon"], a["address_norm"] = g[0], g[1], g[2]
                coords.append((g[0], g[1]))
            else:
                a["lat"], a["lon"] = None, None
                coords.append(None)

        # naive sequence cost (as-is order)
        for i in range(len(coords) - 1):
            if coords[i] and coords[i+1]:
                dm = distance_matrix(coords[i], coords[i+1])
                if dm:
                    appts[i]["drive_to_next_sec"] = dm["seconds"]
                    appts[i]["drive_to_next_text"] = dm["text_duration"]
                    appts[i]["drive_to_next_m"] = dm["meters"]
                    drive_time_total += dm["seconds"]

    return _json_ok({
        "ok": True,
        "therapist": therapist or None,
        "date": date,
        "drive_time": bool(drive_time_total),
        "drive_time_seconds": drive_time_total,
        "appointments": appts
    })

# --- Simple token-gated Schedule UI ---
UI_SCHEDULE = """
<!doctype html>
<meta charset="utf-8"><title>Schedule</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
body{font:15px system-ui;margin:16px}
h1{margin:0 0 10px}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,button,select{padding:8px;inherit:inherit}
.card{border:1px solid #ddd;border-radius:8px;padding:10px;margin:8px 0}
.badge{background:#eef;padding:2px 6px;border-radius:6px}
.small{color:#666;font-size:13px}
.note{color:#444}
</style>
<h1>Day Schedule</h1>
<div class=controls>
  <label>Date <input id="d" type=date></label>
  <label>Therapist <input id="t" placeholder="(optional)"></label>
  <button onclick="load()">Load</button>
  <button onclick="opt()">Optimize</button>
  <span id="tokmsg" class="small"></span>
</div>
<div id="list"></div>
<script>
const fmt = ts => new Date(ts*1000).toLocaleString();

function getCookie(name){
  const m = document.cookie.match(new RegExp('(^| )'+name+'=([^;]+)'));
  return m ? decodeURIComponent(m[2]) : '';
}

async function ensureToken(){
  let tok = getCookie('debug_token');
  if(!tok){
    const q = new URLSearchParams(location.search).get('token') || '';
    if(q){ document.cookie = 'debug_token='+encodeURIComponent(q)+'; Max-Age=2592000; path=/'; tok=q; }
  }
  if(!tok){
    const t = prompt('Enter access token'); 
    if(t){ document.cookie = 'debug_token='+encodeURIComponent(t)+'; Max-Age=2592000; path=/'; return t; }
  }
  return tok;
}

async function autoload(){
  const today = new Date().toISOString().slice(0,10);
  document.getElementById('d').value = today;
  const tok = await ensureToken();
  document.getElementById('tokmsg').textContent = tok ? 'Token set in cookie for 30d' : 'No token';
}
autoload();

async function load(){
  const date = document.getElementById('d').value || new Date().toISOString().slice(0,10);
  const t = document.getElementById('t').value.trim();
  const tok = getCookie('debug_token');
  try{
    const r = await fetch(`/schedule?date=${encodeURIComponent(date)}&therapist=${encodeURIComponent(t)}`,{
      headers:{'X-Debug-Token': tok||''}
    });
    if(!r.ok){ throw new Error(await r.text()); }
    const j = await r.json();
    const L = document.getElementById('list'); L.innerHTML='';
    if(!j.appointments || !j.appointments.length){
      const d = document.createElement('div'); d.className='small note';
      d.textContent = 'No appointments yet. Use Optimize to compute drive-time if you add addresses.';
      L.appendChild(d); return;
    }
    j.appointments.forEach(a=>{
      const d = document.createElement('div'); d.className='card';
      d.innerHTML = `
        <div><b>${a.patient_name||'(unknown)'}</b> <span class=badge>${a.status||''}</span></div>
        <div class=small>${a.start_iso||''}</div>
        <div class=small>${a.address_norm||a.address||''}</div>
        <div class=small>${(a.lat!=null&&a.lon!=null)?('Lat,Lon: '+a.lat+','+a.lon):''}</div>
        <div class=small>${a.patient_phone?('<a href="tel:'+a.patient_phone+'">Call</a>'):''}
            ${(a.lat!=null&&a.lon!=null)?(' · <a target=_blank href="https://maps.google.com/?q='+a.lat+','+a.lon+'">Map</a>'):''}
        </div>
        <div class=small>${a.drive_to_next_text?('Drive to next: '+a.drive_to_next_text):''}</div>
      `;
      L.appendChild(d);
    });
  }catch(e){
    alert('Load error: '+e.message);
  }
}

async function opt(){
  const date = document.getElementById('d').value || new Date().toISOString().slice(0,10);
  const t = document.getElementById('t').value.trim();
  const tok = getCookie('debug_token');
  // In this first pass we just send an empty appointments list; later you’ll pass real stops.
  const body = {date, therapist: t, appointments: []};
  try{
    const r = await fetch('/schedule/optimize',{
      method:'POST',
      headers:{'X-Debug-Token': tok||'', 'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    if(!r.ok){ throw new Error(await r.text()); }
    const j = await r.json();
    alert('Optimized. Drive-time computed: '+(j.drive_time ? 'YES':'NO'));
  }catch(e){
    alert('Optimize error: '+e.message);
  }
}
</script>
"""

@app.route("/ui/schedule", methods=["GET"])
def ui_schedule():
    # Gate with token: header/query/cookie all allowed.
    token = _get_token_from_request()
    if DEBUG_TOKEN and token != DEBUG_TOKEN and not request.cookies.get("debug_token"):
        # Show a minimal HTML prompt if no cookie and wrong/no token
        html = """
        <h2>Enter Access Token</h2>
        <form method="get">
          <input name="token" placeholder="DEBUG_TOKEN" style="padding:8px" size=40>
          <button style="padding:8px">Access</button>
        </form>
        """
        return make_response(html, 401)
    # if a ?token= is provided, set a cookie then render the UI
    resp = make_response(UI_SCHEDULE, 200)
    if token:
        resp.set_cookie("debug_token", token, max_age=60*60*24*30, httponly=False, secure=True, samesite="Lax")
    return resp

# --- Metrics ---
@app.route("/metrics", methods=["GET"])
def metrics_json():
    # safe JSON (not Prometheus). Good for quick checks.
    return _json_ok({
        "status": "ok",
        "service": "Home Health Assistant",
        "mode": "mock" if not TWILIO_READY else "live",
        "uptime_seconds": _uptime_seconds(),
        "routes": {
            "/": {"requests": 0, "errors": 0},
        },
        "sms": {"twilio_ready": TWILIO_READY},
        "maps": {"maps_ready": MAPS_READY},
    })

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    # micro Prometheus-format snapshot
    lines = [
        f'hha_uptime_seconds {_uptime_seconds()}',
        f'hha_twilio_ready {1 if TWILIO_READY else 0}',
        f'hha_maps_ready {1 if MAPS_READY else 0}',
    ]
    return Response("\n".join(lines) + "\n", mimetype="text/plain; charset=utf-8")

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    # Local dev only
    app.run(host="0.0.0.0", port=5000, debug=False)