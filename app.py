import os, json, time, typing, hashlib, hmac
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response, abort, make_response, render_template_string

# ---- Optional Twilio deps (safe in mock mode) ----
Client = None
RequestValidator = None
MessagingResponse = None
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator as TwilioRequestValidator
    from twilio.twiml.messaging_response import MessagingResponse as TwilioMessagingResponse
    Client = TwilioClient
    RequestValidator = TwilioRequestValidator
    MessagingResponse = TwilioMessagingResponse
except Exception:
    pass  # stay mock-safe

app = Flask(__name__)

# ---- Config & Env ----
TWILIO_ACCOUNT_SID       = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN        = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()

STORE_BACKEND            = os.getenv("STORE_BACKEND", "json").strip().lower()  # json (default) | sqlite (future)
ADMIN_PAGE_SIZE          = int(os.getenv("ADMIN_PAGE_SIZE", "50"))
RATE_LIMIT_WINDOW_SEC    = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_MAX_SIMULATE  = int(os.getenv("RATE_LIMIT_MAX_SIMULATE", "30"))

DEBUG_USER               = os.getenv("DEBUG_USER", "").strip()
DEBUG_TOKEN              = os.getenv("DEBUG_TOKEN", "").strip()

SERVICE_NAME             = "Home Health Assistant"
REGION                   = "local"  # Render doesn’t expose region to the dyno, keep simple
BUILD_ID                 = os.getenv("RENDER_GIT_COMMIT", "") or os.getenv("SOURCE_VERSION", "") or "local"
GIT_COMMIT               = (BUILD_ID[:7] if BUILD_ID and BUILD_ID != "local" else "local")
VERSION                  = "1.1.0"  # bump when we ship features

START_TS                 = time.time()

TWILIO_READY             = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE, Client])
twilio_client            = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_READY else None
validator                = RequestValidator(TWILIO_AUTH_TOKEN) if (TWILIO_READY and RequestValidator) else None

# ---- lightweight JSON store ----
STORE_PATH = "store.json"
if not os.path.exists(STORE_PATH):
    with open(STORE_PATH, "w") as f:
        json.dump({"messages": [], "events": []}, f)

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def load_store():
    with open(STORE_PATH, "r") as f:
        return json.load(f)

def save_store(data):
    with open(STORE_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False)

def log_event(kind: str, payload: dict):
    data = load_store()
    data["events"].append({"ts": time.time(), "kind": kind, "payload": payload})
    # keep store from growing unbounded
    if len(data["events"]) > 2000:
        data["events"] = data["events"][-2000:]
    save_store(data)

def log_message(direction: str, body: str, to: str, note: str = "", meta: dict | None = None):
    data = load_store()
    data["messages"].append({
        "ts": time.time(),
        "direction": direction,   # in | out
        "kind": meta.get("kind") if meta else "live",
        "to": to,
        "body": body,
        "note": note,
        "meta": meta or {}
    })
    if len(data["messages"]) > 5000:
        data["messages"] = data["messages"][-5000:]
    save_store(data)

# ---- tiny intent detector ----
def detect_intent(text: str) -> str:
    t = (text or "").strip().lower()
    if any(k in t for k in ["confirm", "yes", "yep", "ok", "okay"]):
        return "confirm"
    if any(k in t for k in ["resched", "another time", "different time", "move"]):
        return "reschedule"
    return "other"

# ---- metrics counters ----
COUNTERS = {
    "requests": 0,
    "errors": 0,
    "avg_latency_seconds": 0.0
}
def track_latency(start_time):
    dt = time.time() - start_time
    COUNTERS["requests"] += 1
    # running average
    n = COUNTERS["requests"]
    COUNTERS["avg_latency_seconds"] = ((COUNTERS["avg_latency_seconds"] * (n - 1)) + dt) / n
    return dt

# ---- helpers ----
def ok(payload: dict, status=200):
    return jsonify(payload), status

def require_debug_token(req) -> bool:
    # Accept either header X-Debug-Token or query ?token=
    token = req.headers.get("X-Debug-Token") or req.args.get("token")
    return DEBUG_TOKEN and token == DEBUG_TOKEN

# ------------------- ROUTES -------------------

@app.route("/", methods=["GET"])
def root():
    return ok({"service": SERVICE_NAME, "status": "ok", "mode": "mock" if not TWILIO_READY else "live"})

@app.route("/healthz", methods=["GET"])
def healthz():
    return ok({
        "mode": "mock" if not TWILIO_READY else "live",
        "service": SERVICE_NAME,
        "status": "ok",
        "twilio_ready": TWILIO_READY,
        "uptime_seconds": round(time.time() - START_TS, 2),
    })

@app.route("/metrics", methods=["GET"])
def metrics():
    return ok({
        "status": "ok",
        "service": SERVICE_NAME,
        "mode": "mock" if not TWILIO_READY else "live",
        "uptime_seconds": round(time.time() - START_TS, 2),
        "routes": {
            "/": {},
            "/healthz": {},
            "/metrics": COUNTERS,
            "/metrics.prom": {},
            "/simulate-sms": {},
            "/send-sms": {},
            "/inbound-sms": {},
            "/status-callback": {},
        },
        "build_id": BUILD_ID,
        "region": REGION,
        "git_commit": GIT_COMMIT
    })

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    body = [
        f'# HELP hha_requests_total Total HTTP requests',
        f'# TYPE hha_requests_total counter',
        f'hha_requests_total {COUNTERS["requests"]}',
        f'# HELP hha_errors_total Total HTTP errors',
        f'# TYPE hha_errors_total counter',
        f'hha_errors_total {COUNTERS["errors"]}',
        f'# HELP hha_avg_latency_seconds Average request latency',
        f'# TYPE hha_avg_latency_seconds gauge',
        f'hha_avg_latency_seconds {COUNTERS["avg_latency_seconds"]:.6f}',
    ]
    return Response("\n".join(body) + "\n", mimetype="text/plain")

# ---- SIMULATED inbound for local & pre-A2P
@app.route("/simulate-sms", methods=["POST"])
def simulate_sms():
    t0 = time.time()
    try:
        payload = request.get_json(force=True, silent=True) or {}
        body = payload.get("body", "")
        frm  = payload.get("from", "+19998887777")
        intent = detect_intent(body)
        log_message("in", body, frm, note="simulate-in", meta={"kind": "simulate", "intent": intent})
        track_latency(t0)
        return ok({"echo": body, "intent": intent, "ok": True})
    except Exception as e:
        COUNTERS["errors"] += 1
        track_latency(t0)
        return ok({"ok": False, "error": "simulate_error", "message": str(e)}, 500)

# ---- OUTBOUND send using Twilio when ready, else mock
@app.route("/send-sms", methods=["POST"])
def send_sms():
    t0 = time.time()
    payload = request.get_json(force=True, silent=True) or {}
    to = payload.get("to", "")
    body = payload.get("body", "")
    if not to or not body:
        COUNTERS["errors"] += 1
        return ok({"ok": False, "error": "missing_fields"}, 400)

    if TWILIO_READY:
        try:
            callback = request.url_root.rstrip("/") + "/status-callback"
            msg = twilio_client.messages.create(
                messaging_service_sid=TWILIO_MESSAGING_SERVICE,
                to=to, body=body, status_callback=callback
            )
            log_message("out", body, to, note="twilio", meta={"sid": msg.sid, "kind": "live"})
            track_latency(t0)
            return ok({"ok": True, "sid": msg.sid, "status": "queued"})
        except Exception as e:
            COUNTERS["errors"] += 1
            track_latency(t0)
            return ok({"ok": False, "error": "twilio_send_error", "message": str(e)}, 502)
    else:
        fake_sid = f"mock-{int(time.time()*1000)}"
        log_message("out", body, to, note="auto-confirm", meta={"sid": fake_sid, "kind": "mock"})
        track_latency(t0)
        return ok({"sid": fake_sid, "status": "mock-sent"})

# ---- NEW: Twilio webhook intake
@app.route("/inbound-sms", methods=["POST"])
def inbound_sms():
    """
    Twilio will POST application/x-www-form-urlencoded
    Fields of interest: From, To, Body, MessageSid, SmsSid
    """
    t0 = time.time()
    form = request.form.to_dict()
    # Optional request validation (only if token & validator available)
    if validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        url = request.url  # full URL Twilio hit
        if not validator.validate(url, form, signature):
            COUNTERS["errors"] += 1
            # respond 403 to non-Twilio callers
            return ok({"ok": False, "error": "invalid_signature"}, 403)

    frm   = form.get("From", "")
    to    = form.get("To", "")
    body  = form.get("Body", "") or ""
    sid   = form.get("MessageSid") or form.get("SmsSid") or ""

    intent = detect_intent(body)
    meta = {"kind": "live" if TWILIO_READY else "simulate", "intent": intent, "sid": sid}
    log_message("in", body, frm, note="twilio-in", meta=meta)

    # Build a TwiML auto-reply (Twilio will deliver this as the reply message)
    reply_text = "Thanks! See you at the scheduled time." if intent == "confirm" \
                 else "Got it — reply with a preferred time to reschedule." if intent == "reschedule" \
                 else "Thanks, we’ll follow up if needed."

    if MessagingResponse:
        twiml = MessagingResponse()
        twiml.message(reply_text)
        # Return TwiML XML
        track_latency(t0)
        return Response(str(twiml), mimetype="application/xml")
    else:
        # Fallback plain text
        track_latency(t0)
        return Response(reply_text, mimetype="text/plain")

# ---- Twilio delivery status callback (for /send-sms API sends)
@app.route("/status-callback", methods=["POST"])
def status_callback():
    form = request.form.to_dict()
    log_event("status_callback", form)
    return ("", 204)

# ---- Minimal Admin JSON endpoints (unchanged) ----
@app.route("/admin/messages", methods=["GET"])
def admin_messages():
    if not require_debug_token(request): return abort(403)
    limit = max(1, min(500, int(request.args.get("limit", ADMIN_PAGE_SIZE))))
    q     = (request.args.get("q") or "").lower().strip()
    data = load_store()
    msgs = data.get("messages", [])
    if q:
        msgs = [m for m in msgs if q in (m.get("body","").lower())]
    msgs = list(sorted(msgs, key=lambda m: m["ts"], reverse=True))[:limit]
    return ok({"messages": msgs})

@app.route("/admin/export.csv", methods=["GET"])
def admin_export_csv():
    if not require_debug_token(request): return abort(403)
    import io, csv
    data = load_store().get("messages", [])
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ts","direction","kind","to","body","note"])
    for m in data:
        w.writerow([m["ts"], m["direction"], m.get("kind",""), m["to"], m["body"], m.get("note","")])
    resp = make_response(out.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = 'attachment; filename="export.csv"'
    return resp

# ---- Simple Admin UI (unchanged)
ADMIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Home Health Assistant — Admin</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:16px}
table{border-collapse:collapse;width:100%} th,td{border:1px solid #ddd;padding:8px} th{background:#fafafa;text-align:left}
input[type=text]{padding:6px;width:220px} .muted{color:#777}
</style></head><body>
<h1>Home Health Assistant — Admin</h1>
<div>
  Token <input id="tok" value="{{token}}" style="width:360px"> &nbsp;
  Search <input id="q" placeholder="text filter"> &nbsp;
  Limit <input id="lim" value="{{limit}}" size="4">
  <button onclick="load()">Load</button>
  <button onclick="exp()">Export CSV</button>
</div>
<div class="muted" id="count"></div>
<table id="t"><thead><tr>
  <th>body</th><th>direction</th><th>kind</th><th>note</th><th>to</th><th>ts</th>
</tr></thead><tbody></tbody></table>
<script>
async function load(){
  const tok=document.getElementById('tok').value;
  const q=document.getElementById('q').value;
  const lim=document.getElementById('lim').value||50;
  const r=await fetch(`/admin/messages?limit=${lim}&q=${encodeURIComponent(q)}`,{headers:{'X-Debug-Token':tok}});
  if(r.status!=200){alert('Auth failed');return;}
  const j=await r.json(); const tb=document.querySelector('#t tbody'); tb.innerHTML='';
  document.getElementById('count').textContent=j.messages.length+' messages';
  for(const m of j.messages){
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${m.body||''}</td><td>${m.direction}</td><td>${(m.meta&&m.meta.kind)||''}</td>
                  <td>${m.note||''}</td><td>${m.to||''}</td><td>${m.ts}</td>`;
    tb.appendChild(tr);
  }
}
function exp(){
  const tok=document.getElementById('tok').value;
  window.location=`/admin/export.csv`; fetch('/admin/export.csv',{headers:{'X-Debug-Token':tok}});
}
load();
</script>
</body></html>
"""

@app.route("/admin", methods=["GET"])
def admin_page():
    if not require_debug_token(request): return abort(403)
    return render_template_string(ADMIN_HTML, token=DEBUG_TOKEN, limit=ADMIN_PAGE_SIZE)

# ---- Debug dashboard (unchanged)
@app.route("/debug", methods=["GET"])
def debug_dashboard():
    token_ok = (request.args.get("token")==DEBUG_TOKEN) or require_debug_token(request)
    if not token_ok: return abort(403)
    info = {
        "mode": "mock" if not TWILIO_READY else "live",
        "uptime_sec": round(time.time()-START_TS,2),
        "twilio_ready": TWILIO_READY,
        "region": REGION,
        "build_id": BUILD_ID,
        "git_commit": GIT_COMMIT,
        "version": VERSION,
        "build_time": _now_iso()
    }
    html = f"""
    <h1 style="font-family:system-ui">Home Health Assistant Debug Dashboard</h1>
    <p>Status: <b style="color:green">ok</b></p>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><td>MODE</td><td>{info['mode']}</td></tr>
      <tr><td>UPTIME (SEC)</td><td>{info['uptime_sec']}</td></tr>
      <tr><td>TWILIO READY</td><td>{info['twilio_ready']}</td></tr>
      <tr><td>REGION</td><td>{info['region']}</td></tr>
      <tr><td>BUILD ID</td><td>{info['build_id']}</td></tr>
      <tr><td>GIT COMMIT</td><td>{info['git_commit']}</td></tr>
      <tr><td>VERSION</td><td>{info['version']}</td></tr>
      <tr><td>BUILD TIME</td><td>{info['build_time']}</td></tr>
    </table>
    <p class="muted">Tip: You can also use a query token if configured: <code>/debug?token=YOUR_TOKEN</code></p>
    """
    return html

# ---- Flask app end ----