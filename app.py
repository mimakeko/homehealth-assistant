import os, re, json, time, csv, io, hashlib, datetime
from pathlib import Path
from flask import Flask, request, jsonify, Response, make_response

# ---------- Config ----------
APP_NAME = "Home Health Assistant"
app = Flask(__name__)

# Twilio (optional in mock mode)
try:
    from twilio.rest import Client  # noqa
except Exception:
    Client = None

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
TWILIO_READY = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID])

DEBUG_USER = os.getenv("DEBUG_USER", "").strip()        # optional
DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "").strip()      # set in Render
STORE_BACKEND = (os.getenv("STORE_BACKEND") or "json").lower()  # "json" for now
ADMIN_PAGE_SIZE = int(os.getenv("ADMIN_PAGE_SIZE", "50"))
RATE_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_MAX_SIM = int(os.getenv("RATE_LIMIT_MAX_SIMULATE", "30"))

START_TS = time.time()
BUILD_ID = hashlib.sha1(f"{START_TS}".encode()).hexdigest()[:16]
REGION = os.getenv("RENDER_REGION", "local")
GIT_COMMIT = os.getenv("RENDER_GIT_COMMIT", "")[:7] if os.getenv("RENDER_GIT_COMMIT") else ""
VERSION = "1.0.1"
BUILD_TIME = datetime.datetime.utcnow().isoformat()

# ---------- Storage (JSONL) ----------
STORE_DIR = Path("store")
STORE_DIR.mkdir(exist_ok=True)
MSG_FILE = STORE_DIR / "messages.jsonl"
RATE_FILE = STORE_DIR / "rate.json"

def _read_all_messages():
    if not MSG_FILE.exists():
        return []
    out = []
    with MSG_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                # keep going even if 1 bad line
                pass
    return out

def _append_message(rec: dict):
    rec["ts"] = time.time()
    with MSG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def _count_recent_simulations():
    now = time.time()
    start = now - RATE_WINDOW
    count = 0
    for m in _read_all_messages():
        if m.get("kind") == "simulate" and m.get("ts", 0) >= start:
            count += 1
    return count

# ---------- Intent detection ----------
YES_RE = re.compile(r"\b(yes|yeah|yep|confirm|ok|okay|si|sim)\b", re.I)
NO_RE  = re.compile(r"\b(no|nope|nao|não|cancel)\b", re.I)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)

def detect_intent(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "other"
    if YES_RE.search(t):
        return "confirm"
    if NO_RE.search(t):
        return "decline"
    if TIME_RE.search(t):
        return "time"
    return "other"

# ---------- Security helpers ----------
def _auth_ok(req) -> bool:
    # header wins, then query string
    token = req.headers.get("X-Debug-Token") or req.args.get("token") or ""
    return (DEBUG_TOKEN and token == DEBUG_TOKEN)

def _require_auth():
    if not _auth_ok(request):
        return make_response(("Unauthorized", 401, {"WWW-Authenticate": "Bearer"}))
    return None

# ---------- Health & Metrics ----------
def uptime_seconds() -> float:
    return round(time.time() - START_TS, 2)

def service_snapshot(status="ok"):
    return {
        "mode": "live" if TWILIO_READY else "mock",
        "service": APP_NAME,
        "status": status,
        "twilio_ready": TWILIO_READY,
        "uptime_seconds": uptime_seconds()
    }

@app.route("/", methods=["GET"])
def root():
    return jsonify({k: service_snapshot()[k] for k in ("service","status","mode")})

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify(service_snapshot())

# Prometheus-style counters (very light)
COUNTERS = {
    "requests_total": 0,
    "errors_total": 0,
    "avg_latency_seconds": 0.0,
    "sms_out_total": 0,
}

@app.before_request
def _before():
    request._t0 = time.time()
    COUNTERS["requests_total"] += 1

@app.after_request
def _after(resp):
    dt = max(0.0, time.time() - getattr(request, "_t0", time.time()))
    # online incremental avg
    n = max(1, COUNTERS["requests_total"])
    COUNTERS["avg_latency_seconds"] = COUNTERS["avg_latency_seconds"] + (dt - COUNTERS["avg_latency_seconds"])/n
    return resp

@app.errorhandler(Exception)
def _err(e):
    COUNTERS["errors_total"] += 1
    return (f"<h1>Internal Server Error</h1>", 500)

@app.route("/metrics", methods=["GET"])
def metrics_json():
    return jsonify({
        "status": "ok",
        "service": APP_NAME,
        "mode": "live" if TWILIO_READY else "mock",
        "uptime_seconds": uptime_seconds(),
        "healthz": COUNTERS,
        "routes": {"/": {}, "/healthz": {}, "/metrics": {}, "/simulate-sms": {}, "/send-sms": {}},
        "build": {
            "build_id": BUILD_ID, "region": REGION, "git_commit": GIT_COMMIT or "local",
            "service": APP_NAME, "version": VERSION, "build_time": BUILD_TIME
        }
    })

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    # return only simple numeric lines (Prom format)
    lines = [
        f'homehealth_requests_total {COUNTERS["requests_total"]}',
        f'homehealth_errors_total {COUNTERS["errors_total"]}',
        f'homehealth_avg_latency_seconds {COUNTERS["avg_latency_seconds"]:.6f}',
        f'homehealth_uptime_seconds {uptime_seconds():.2f}',
        f'homehealth_sms_out_total {COUNTERS["sms_out_total"]}',
    ]
    return Response("\n".join(lines) + "\n", mimetype="text/plain")

# ---------- SMS simulation & send ----------
@app.route("/simulate-sms", methods=["POST"])
def simulate_sms():
    # Rate limit for simulate
    if _count_recent_simulations() >= RATE_MAX_SIM:
        return jsonify({"ok": False, "error": "rate_limited", "window_sec": RATE_WINDOW}), 429

    payload = request.get_json(silent=True) or {}
    from_num = (payload.get("from") or "").strip()
    body = (payload.get("body") or "").strip()

    intent = detect_intent(body)
    _append_message({"kind": "simulate", "direction": "in", "from": from_num, "body": body, "intent": intent})

    # NEW: auto-confirm loopback (mock) — if confirm, log an outbound “thanks”
    if intent == "confirm":
        thanks = "Thanks! See you at the scheduled time."
        _append_message({"kind": "simulate", "direction": "out", "to": from_num, "body": thanks, "note": "auto-confirm"})
        COUNTERS["sms_out_total"] += 1

    return jsonify({"ok": True, "echo": body, "intent": intent})

@app.route("/send-sms", methods=["POST"])
def send_sms():
    payload = request.get_json(silent=True) or {}
    to = (payload.get("to") or "").strip()
    body = (payload.get("body") or "").strip()

    if not to or not body:
        return jsonify({"ok": False, "error": "missing_fields"}), 400

    if not TWILIO_READY or Client is None:
        # mock success
        sid = f"mock-{int(time.time()*1000)}"
        _append_message({"kind": "send", "direction": "out", "to": to, "body": body, "sid": sid, "mock": True})
        COUNTERS["sms_out_total"] += 1
        return jsonify({"sid": sid, "status": "mock-sent"})

    # (When campaign is approved, we’ll send via Twilio client here)
    try:
        tw = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = tw.messages.create(messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID, to=to, body=body)
        _append_message({"kind": "send", "direction": "out", "to": to, "body": body, "sid": msg.sid, "mock": False})
        COUNTERS["sms_out_total"] += 1
        return jsonify({"sid": msg.sid, "status": msg.status or "sent"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---------- Admin APIs (token protected) ----------
@app.route("/admin/messages", methods=["GET"])
def admin_messages():
    unauth = _require_auth()
    if unauth: return unauth
    limit = max(1, min(int(request.args.get("limit", ADMIN_PAGE_SIZE)), 500))
    q = (request.args.get("q") or "").lower().strip()
    items = list(reversed(_read_all_messages()))
    if q:
        items = [m for m in items if q in json.dumps(m).lower()]
    return jsonify({"count": len(items[:limit]), "items": items[:limit]})

@app.route("/admin/intents", methods=["GET"])
def admin_intents():
    unauth = _require_auth()
    if unauth: return unauth
    tally = {}
    for m in _read_all_messages():
        if "intent" in m:
            tally[m["intent"]] = tally.get(m["intent"], 0) + 1
    intents = [{"intent": k, "count": v} for k, v in sorted(tally.items(), key=lambda x: -x[1])]
    return jsonify({"intents": intents})

@app.route("/admin/export.csv", methods=["GET"])
def admin_export_csv():
    unauth = _require_auth()
    if unauth: return unauth
    rows = _read_all_messages()
    # CSV to browser download
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=sorted({k for r in rows for k in r.keys()}))
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = 'attachment; filename="export.csv"'
    return resp

# ---------- Admin Web UI (token protected) ----------
ADMIN_HTML = """
<!doctype html>
<meta charset="utf-8">
<title>Home Health Assistant — Admin</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu; margin:24px;}
h1{margin:0 0 16px}
input,button{font-size:14px;padding:8px}
table{border-collapse:collapse;width:100%;margin-top:12px}
th,td{border:1px solid #ddd;padding:6px 8px;font-size:13px}
th{background:#f6f6f6;text-align:left}
.badge{display:inline-block;padding:.15rem .4rem;border-radius:.35rem;background:#eef}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.small{color:#666;font-size:12px}
</style>
<h1>Home Health Assistant — Admin</h1>
<div class="controls">
  <label>Token <input id="token" placeholder="token" /></label>
  <label>Search <input id="q" placeholder="text filter" /></label>
  <label>Limit <input id="limit" type="number" value="50" style="width:80px"/></label>
  <button onclick="load()">Load</button>
  <button onclick="downloadCSV()">Export CSV</button>
  <span id="summary" class="small"></span>
</div>
<div id="intents"></div>
<table id="tbl"><thead><tr></tr></thead><tbody></tbody></table>
<script>
async function load(){
  const token=document.getElementById('token').value.trim();
  const q=document.getElementById('q').value.trim();
  const lim=document.getElementById('limit').value||50;

  const h={ 'X-Debug-Token': token };
  const base=window.location.origin;

  const [msgs,ints]=await Promise.all([
    fetch(`${base}/admin/messages?limit=${lim}&q=${encodeURIComponent(q)}`,{headers:h}).then(r=>r.json()),
    fetch(`${base}/admin/intents`,{headers:h}).then(r=>r.json())
  ]);

  document.getElementById('summary').textContent=`${msgs.count} messages`;
  const ic = (ints.intents||[]).map(i=>`<span class="badge">${i.intent}: ${i.count}</span>`).join(' ');
  document.getElementById('intents').innerHTML = ic;

  const rows=msgs.items||[];
  const cols = Array.from(rows.reduce((s,r)=>{Object.keys(r).forEach(k=>s.add(k));return s;}, new Set()));
  const thead=document.querySelector('#tbl thead tr'); thead.innerHTML="";
  cols.forEach(c=>{ const th=document.createElement('th'); th.textContent=c; thead.appendChild(th); });

  const tb=document.querySelector('#tbl tbody'); tb.innerHTML="";
  rows.forEach(r=>{
    const tr=document.createElement('tr');
    cols.forEach(c=>{
      const td=document.createElement('td');
      td.textContent=(r[c]!==undefined)? String(r[c]) : "";
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
}
async function downloadCSV(){
  const token=document.getElementById('token').value.trim();
  const base=window.location.origin;
  const a=document.createElement('a');
  a.href=`${base}/admin/export.csv`;
  a.download="export.csv";
  a.target="_blank";
  a.rel="noopener";
  a.click();
}
</script>
"""

@app.route("/admin", methods=["GET"])
def admin_page():
    # Don’t hard-block UI for token; page loads, but API calls require token header.
    return Response(ADMIN_HTML, mimetype="text/html")

# ---------- Debug dashboard (token optional) ----------
@app.route("/debug", methods=["GET"])
def debug_dash():
    token_ok = _auth_ok(request) if DEBUG_TOKEN else True
    color = "#0a0" if token_ok else "#a00"
    snap = service_snapshot()
    rows = [
        ("MODE", snap["mode"]),
        ("UPTIME (SEC)", snap["uptime_seconds"]),
        ("TWILIO READY", snap["twilio_ready"]),
        ("REGION", REGION),
        ("BUILD ID", BUILD_ID),
        ("GIT COMMIT", GIT_COMMIT or "local"),
        ("VERSION", VERSION),
        ("BUILD TIME", BUILD_TIME),
    ]
    html = [
        "<!doctype html><meta charset='utf-8'><title>Debug</title>",
        "<style>body{font:14px system-ui;margin:24px} table{border-collapse:collapse} td{border:1px solid #ddd;padding:6px 10px}</style>",
        f"<h1>{APP_NAME} Debug Dashboard</h1>",
        f"<p>Status: <b style='color:{color}'>{snap['status']}</b></p>",
        "<table>",
    ]
    for k,v in rows:
        html.append(f"<tr><td>{k}</td><td>{v}</td></tr>")
    html.append("</table>")
    if DEBUG_TOKEN:
        html.append("<p class='small'>Tip: You can also use a query token if configured: <code>/debug?token=YOUR_TOKEN</code></p>")
    return Response("\n".join(html), mimetype="text/html")