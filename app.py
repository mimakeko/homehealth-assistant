import os
import json
import time
import base64
import csv
from datetime import datetime, timezone
from collections import deque, defaultdict
from flask import Flask, request, jsonify, Response, abort, make_response

# -------------------------------
# Optional Twilio client (only if creds are present)
# -------------------------------
Client = None
try:
    from twilio.rest import Client as TwilioClient  # type: ignore
    Client = TwilioClient
except Exception:
    Client = None

app = Flask(__name__)

# -------------------------------
# Env & Config
# -------------------------------
FLASK_ENV = os.getenv("FLASK_ENV", "production")
PY_VERSION = os.getenv("PYTHON_VERSION", "").strip() or "3.x"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()

STORE_BACKEND = os.getenv("STORE_BACKEND", "json").strip().lower()  # json | sqlite (json for now)
ADMIN_PAGE_SIZE = int(os.getenv("ADMIN_PAGE_SIZE", "50"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_MAX_SIMULATE = int(os.getenv("RATE_LIMIT_MAX_SIMULATE", "30"))

DEBUG_USER = os.getenv("DEBUG_USER", "").strip()
DEBUG_PASS = os.getenv("DEBUG_PASS", "").strip()  # optional, only if you want Basic Auth password
DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "").strip()

BUILD_REGION = os.getenv("RENDER_REGION", "local")
BUILD_ID = os.getenv("RENDER_SERVICE_ID", "local")
GIT_COMMIT = os.getenv("GIT_COMMIT", os.getenv("RENDER_GIT_COMMIT", "local"))[:7] or "local"
VERSION = os.getenv("APP_VERSION", "1.0.0")

APP_START = time.time()

# -------------------------------
# Twilio readiness
# -------------------------------
TWILIO_READY = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_MESSAGING_SERVICE_SID and Client)
twilio_client = None
if TWILIO_READY:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)  # type: ignore
    except Exception:
        TWILIO_READY = False
        twilio_client = None

MODE = "live" if TWILIO_READY else "mock"

# -------------------------------
# Simple JSONL store
# -------------------------------
DATA_FILE = "messages.jsonl"

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def jsonl_append(record: dict) -> None:
    if STORE_BACKEND != "json":
        return
    rec = dict(record)
    rec.setdefault("ts", _now_iso())
    line = json.dumps(rec, ensure_ascii=False)
    with open(DATA_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def jsonl_tail(n: int):
    """Return last n records (efficient-ish for modest files)."""
    if not os.path.exists(DATA_FILE):
        return []
    dq = deque(maxlen=n)
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                dq.append(json.loads(line))
            except Exception:
                continue
    return list(dq)

def jsonl_all():
    if not os.path.exists(DATA_FILE):
        return []
    out = []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out

# -------------------------------
# Auth helpers for /debug and /admin/*
# -------------------------------
def _is_authorized(req: request) -> bool:
    # Token in query string
    token_qs = req.args.get("token", "")
    if DEBUG_TOKEN and token_qs and token_qs == DEBUG_TOKEN:
        return True

    # Basic Auth header: "Basic base64(user:pass)"
    auth = req.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            raw = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
            user, pwd = raw.split(":", 1)
            if DEBUG_USER and user == DEBUG_USER:
                # If DEBUG_PASS is set, check it. Otherwise permit any password for the known user.
                if DEBUG_PASS:
                    return pwd == DEBUG_PASS
                return True
        except Exception:
            pass

    # Token header
    hdr_token = req.headers.get("X-Debug-Token", "")
    if DEBUG_TOKEN and hdr_token == DEBUG_TOKEN:
        return True

    return False

def require_auth():
    if not _is_authorized(request):
        # 401 with WWW-Authenticate prompts browser for Basic Auth
        resp = make_response("Unauthorized", 401)
        resp.headers["WWW-Authenticate"] = 'Basic realm="HomeHealthAssistant"'
        return resp
    return None

# -------------------------------
# Rate limiting (in-memory)
# -------------------------------
recent_hits = deque()  # timestamps of simulate/send attempts

def rate_limit_guard():
    now = time.time()
    # drop old entries
    while recent_hits and now - recent_hits[0] > RATE_LIMIT_WINDOW_SEC:
        recent_hits.popleft()
    if len(recent_hits) >= RATE_LIMIT_MAX_SIMULATE:
        return False
    recent_hits.append(now)
    return True

# -------------------------------
# Utility: classify very simple "intent" from text
# -------------------------------
def classify_intent(body: str) -> str:
    b = (body or "").strip().lower()
    if not b:
        return "empty"
    keys = {
        "confirm": ["yes", "yep", "confirm", "ok", "okay", "yup", "sure"],
        "reschedule": ["reschedule", "another time", "later", "tomorrow", "move"],
        "cancel": ["cancel", "can't", "cannot", "no"],
        "language": ["spanish", "portuguese", "brazil", "español", "português"],
        "address": ["address", "where", "location"],
    }
    for label, words in keys.items():
        if any(w in b for w in words):
            return label
    return "other"

# -------------------------------
# Root & health
# -------------------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "Home Health Assistant", "status": "ok", "mode": MODE})

@app.route("/healthz", methods=["GET"])
def healthz():
    uptime = max(0.0, time.time() - APP_START)
    return jsonify({
        "mode": MODE,
        "service": "Home Health Assistant",
        "status": "ok",
        "twilio_ready": TWILIO_READY,
        "uptime_seconds": round(uptime, 2),
    })

# -------------------------------
# Metrics (Prometheus text exposition)
# -------------------------------
# These counters are in-memory (reset on dyno restart, ok for now)
counters = defaultdict(int)
latency_hist = deque(maxlen=500)  # store last latencies (ms) for a tiny avg

def _record_metric(name: str, ms: float = None):
    counters[name] += 1
    if ms is not None:
        latency_hist.append(ms)

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    # Basic counters and a simple avg latency
    avg_ms = 0.0
    if latency_hist:
        avg_ms = sum(latency_hist) / len(latency_hist)

    lines = []
    lines.append("# HELP hha_requests_total Total requests by type")
    lines.append("# TYPE hha_requests_total counter")
    for k, v in counters.items():
        lines.append(f'hha_requests_total{{route="{k}"}} {v}')

    lines.append("# HELP hha_avg_latency_ms Average handler latency (approx)")
    lines.append("# TYPE hha_avg_latency_ms gauge")
    # Emit a float safely
    lines.append(f"hha_avg_latency_ms {avg_ms:.3f}")

    # Health summary gauge
    lines.append("# HELP hha_twilio_ready Twilio readiness (1 or 0)")
    lines.append("# TYPE hha_twilio_ready gauge")
    lines.append(f"hha_twilio_ready {1 if TWILIO_READY else 0}")

    body = "\n".join(lines) + "\n"
    return Response(body, mimetype="text/plain")

# -------------------------------
# Debug dashboard (token or basic auth)
# -------------------------------
@app.route("/debug", methods=["GET"])
def debug_dashboard():
    need = require_auth()
    if need:
        return need

    uptime = max(0.0, time.time() - APP_START)
    status = {
        "status": "ok",
        "service": "Home Health Assistant",
        "mode": MODE,
        "uptime_seconds": round(uptime, 2),
        "twilio_ready": TWILIO_READY,
        "region": BUILD_REGION,
        "build_id": BUILD_ID,
        "git_commit": GIT_COMMIT,
        "version": VERSION,
        "build_time": datetime.utcfromtimestamp(APP_START).replace(tzinfo=timezone.utc).isoformat(),
        "tip": "Use /debug?token=YOUR_TOKEN or Basic Auth to access.",
    }

    # minimal HTML table for quick human view
    rows = "\n".join(
        f"<tr><th style='text-align:left;padding:6px 10px;'>{k.upper()}</th>"
        f"<td style='padding:6px 10px;'>{status[k]}</td></tr>"
        for k in [
            "mode", "uptime_seconds", "twilio_ready", "region", "build_id",
            "git_commit", "version", "build_time"
        ]
    )
    html = f"""
    <html><head><title>Home Health Assistant – Debug</title>
    <style>
      body {{ font-family: -apple-system, Segoe UI, Roboto, Arial; margin: 24px; }}
      h1 {{ color: #1145e6; }}
      table {{ border-collapse: collapse; border: 1px solid #ddd; }}
      th, td {{ border: 1px solid #ddd; }}
    </style></head>
    <body>
      <h1>Home Health Assistant Debug Dashboard</h1>
      <p>Status: <b style="color:green">ok</b></p>
      <table>{rows}</table>
      <p style="margin-top:12px;font-size:12px;">Tip: You can also use a query token if configured: <code>/debug?token=YOUR_TOKEN</code></p>
    </body></html>
    """
    return Response(html, mimetype="text/html")

# -------------------------------
# Simulation & sending
# -------------------------------
@app.route("/simulate-sms", methods=["POST"])
def simulate_sms():
    start = time.time()
    if not rate_limit_guard():
        abort(429, description="Rate limit exceeded. Please slow down.")

    data = request.get_json(silent=True) or {}
    from_num = (data.get("from") or data.get("from_number") or "simulated").strip()
    body = (data.get("body") or "").strip()

    intent = classify_intent(body)
    record = {
        "source": "simulate",
        "direction": "inbound",
        "from": from_num,
        "to": "clinic",
        "body": body,
        "intent": intent,
        "twilio_message_sid": None,
        "ts": _now_iso()
    }
    jsonl_append(record)
    _record_metric("simulate_sms", (time.time() - start) * 1000.0)

    return jsonify({"ok": True, "intent": intent, "echo": body})

@app.route("/send-sms", methods=["POST"])
def send_sms():
    start = time.time()
    if not rate_limit_guard():
        abort(429, description="Rate limit exceeded. Please slow down.")

    data = request.get_json(silent=True) or {}
    to_number = (data.get("to") or "").strip()
    body = (data.get("body") or "").strip()
    if not to_number or not body:
        abort(400, description="Missing 'to' or 'body'")

    if TWILIO_READY and twilio_client:
        try:
            msg = twilio_client.messages.create(
                messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
                to=to_number,
                body=body
            )
            sid = msg.sid
            status = "sent-to-twilio"
        except Exception as e:
            sid = None
            status = f"twilio-error: {e}"
    else:
        sid = "mock-" + str(int(time.time()))
        status = "mock-sent"

    record = {
        "source": "api",
        "direction": "outbound",
        "from": "clinic",
        "to": to_number,
        "body": body,
        "intent": classify_intent(body),
        "twilio_message_sid": sid,
        "ts": _now_iso()
    }
    jsonl_append(record)
    _record_metric("send_sms", (time.time() - start) * 1000.0)

    return jsonify({"status": status, "sid": sid})

# -------------------------------
# Twilio webhook placeholder
# -------------------------------
@app.route("/twilio/sms", methods=["POST"])
def twilio_webhook_sms():
    """
    Twilio will POST here later. For now, we accept either real Twilio form
    data or JSON (for tests). We always log the inbound message.
    """
    start = time.time()
    body = request.form.get("Body") or (request.get_json(silent=True) or {}).get("body") or ""
    from_num = request.form.get("From") or (request.get_json(silent=True) or {}).get("from") or "unknown"

    record = {
        "source": "twilio" if TWILIO_READY else "webhook-test",
        "direction": "inbound",
        "from": from_num,
        "to": "clinic",
        "body": body,
        "intent": classify_intent(body),
        "twilio_message_sid": request.form.get("MessageSid"),
        "ts": _now_iso()
    }
    jsonl_append(record)
    _record_metric("twilio_inbound", (time.time() - start) * 1000.0)

    # TwiML minimal response (helps clear Twilio queue if used live)
    resp = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return Response(resp, mimetype="application/xml")

# -------------------------------
# Admin & analytics
# -------------------------------
@app.route("/admin/messages", methods=["GET"])
def admin_messages():
    need = require_auth()
    if need:
        return need

    n = int(request.args.get("limit", str(ADMIN_PAGE_SIZE)))
    msgs = jsonl_tail(max(1, min(n, 500)))
    return jsonify({"count": len(msgs), "messages": msgs})

@app.route("/admin/intents", methods=["GET"])
def admin_intents():
    need = require_auth()
    if need:
        return need

    msgs = jsonl_tail(1000)
    tally = defaultdict(int)
    for m in msgs:
        label = m.get("intent") or classify_intent(m.get("body", ""))
        tally[label] += 1

    # sort desc
    ranked = sorted(tally.items(), key=lambda t: t[1], reverse=True)
    return jsonify({"intents": [{"intent": k, "count": v} for k, v in ranked]})

@app.route("/admin/export.csv", methods=["GET"])
def admin_export_csv():
    need = require_auth()
    if need:
        return need

    rows = jsonl_all()
    # Prepare CSV
    headers = ["ts", "source", "direction", "from", "to", "body", "intent", "twilio_message_sid"]
    def gen():
        yield ",".join(headers) + "\n"
        w = csv.writer(None)  # type: ignore  # just for quoting help
        for r in rows:
            line = [
                r.get("ts", ""),
                r.get("source", ""),
                r.get("direction", ""),
                r.get("from", ""),
                r.get("to", ""),
                r.get("body", "").replace("\n", " ").replace("\r", " "),
                r.get("intent", ""),
                r.get("twilio_message_sid", "") or "",
            ]
            # manual CSV safe quoting:
            out = []
            for cell in line:
                cell = str(cell)
                if any(ch in cell for ch in [",", '"', "\n"]):
                    cell = '"' + cell.replace('"', '""') + '"'
                out.append(cell)
            yield ",".join(out) + "\n"

    return Response(gen(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=messages_export.csv"
    })

# -------------------------------
# Self-test (quick smoke)
# -------------------------------
@app.route("/self-test", methods=["POST"])
def self_test():
    """
    Quick end-to-end: simulate an inbound, then send an outbound (mock if needed)
    """
    start = time.time()
    sim = app.test_client().post("/simulate-sms", json={"from": "tester", "body": "Yes please"})
    snd = app.test_client().post("/send-sms", json={"to": "+10000000000", "body": "Appt confirmed"})
    _record_metric("self_test", (time.time() - start) * 1000.0)
    return jsonify({
        "simulate_status": sim.status_code,
        "send_status": snd.status_code,
        "mode": MODE,
        "twilio_ready": TWILIO_READY
    })

# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    # Local run
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)