import os
import time
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Callable
from functools import wraps

from flask import Flask, request, Response

# --- Optional: Prometheus ---
try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
except Exception:
    CollectorRegistry = Counter = Gauge = generate_latest = None
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

# --- Optional: Twilio ---
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

app = Flask(__name__)

# ---------- metadata ----------
START_TS = time.time()
BUILD_ID = os.getenv("RENDER_GIT_COMMIT", "local")
REGION = os.getenv("RENDER_REGION", "local")
VERSION = os.getenv("APP_VERSION", "1.0.0")
SERVICE_NAME = "Home Health Assistant"

# ---------- env & mode ----------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
TWILIO_READY = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_MESSAGING_SERVICE_SID)

twilio_client = None
if TWILIO_READY and TwilioClient is not None:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        twilio_client = None

MODE = "twilio" if TWILIO_READY and twilio_client else "mock"

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("homehealth")

# ---------- auth config ----------
DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "").strip()           # optional token (?token=XYZ)
DEBUG_USER = os.getenv("DEBUG_USER", "").strip()             # for Basic Auth
DEBUG_PASS = os.getenv("DEBUG_PASS", "").strip()             # for Basic Auth
DEBUG_REALM = os.getenv("DEBUG_REALM", "HomeHealth Debug")   # browser prompt label

def _authorized_via_token() -> bool:
    if not DEBUG_TOKEN:
        return False
    return request.args.get("token", "") == DEBUG_TOKEN

def _authorized_via_basic() -> bool:
    if not (DEBUG_USER and DEBUG_PASS):
        return False
    auth = request.authorization
    return bool(auth and auth.type.lower() == "basic" and auth.username == DEBUG_USER and auth.password == DEBUG_PASS)

def _require_auth(f: Callable) -> Callable:
    """Decorator: allow access if query token OR Basic Auth matches."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _authorized_via_token() or _authorized_via_basic():
            return f(*args, **kwargs)
        # If creds are configured, prompt Basic. If not, show clear message.
        if DEBUG_USER and DEBUG_PASS:
            return Response("Authentication required\n", 401, {"WWW-Authenticate": f'Basic realm="{DEBUG_REALM}"'})
        return Response("Debug auth not configured. Set DEBUG_TOKEN or DEBUG_USER/DEBUG_PASS.\n", 503)
    return wrapper

# ---------- metrics ----------
_has_prom = all([CollectorRegistry, Counter, Gauge, generate_latest])
if _has_prom:
    REGISTRY = CollectorRegistry()
    REQ_COUNT = Counter("hha_requests_total", "Total HTTP requests", ["route", "method", "status"], registry=REGISTRY)
    REQ_ERRORS = Counter("hha_request_errors_total", "Total request errors", ["route"], registry=REGISTRY)
    LATENCY = Gauge("hha_last_request_latency_seconds", "Last request latency (s)", ["route"], registry=REGISTRY)
else:
    REGISTRY = REQ_COUNT = REQ_ERRORS = LATENCY = None

def uptime_seconds() -> float:
    return max(0.0, time.time() - START_TS)

def base_status() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "mode": MODE,
        "uptime_seconds": round(uptime_seconds(), 2),
        "twilio_ready": bool(twilio_client is not None and TWILIO_READY),
    }

def build_info() -> Dict[str, Any]:
    return {
        "build_id": BUILD_ID,
        "region": REGION,
        "version": VERSION,
        "build_time": datetime.now(timezone.utc).isoformat(),
        "git_commit": BUILD_ID[:7] if BUILD_ID != "local" else "dev",
    }

def record_metrics(route: str, method: str, status_code: int, elapsed: float):
    try:
        if _has_prom:
            REQ_COUNT.labels(route=route, method=method, status=status_code).inc()
            LATENCY.labels(route=route).set(max(elapsed, 0.0))
    except Exception:
        pass

def record_error(route: str):
    try:
        if _has_prom:
            REQ_ERRORS.labels(route=route).inc()
    except Exception:
        pass

def json_response(payload: Dict[str, Any], code: int = 200) -> Response:
    return Response(json.dumps(payload), status=code, mimetype="application/json")

# ---------- public routes ----------
@app.route("/", methods=["GET"])
def root():
    return json_response({"service": SERVICE_NAME, "status": "ok", "mode": MODE})

@app.route("/healthz", methods=["GET"])
def healthz():
    t0 = time.time()
    payload = base_status()
    elapsed = time.time() - t0
    record_metrics("/healthz", "GET", 200, elapsed)
    return json_response(payload, 200)

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    """Prometheus exposition. Keep public so uptime monitors can scrape."""
    if not _has_prom or not REGISTRY:
        return Response("# metrics disabled\n", mimetype=CONTENT_TYPE_LATEST, status=200)
    try:
        return Response(generate_latest(REGISTRY), mimetype=CONTENT_TYPE_LATEST, status=200)
    except (BrokenPipeError, ConnectionResetError):
        return Response("", mimetype=CONTENT_TYPE_LATEST, status=200)
    except Exception as e:
        record_error("/metrics.prom")
        return Response(f"# error {e}\n", mimetype=CONTENT_TYPE_LATEST, status=500)

# ---------- protected routes (auth required) ----------
@app.route("/metrics", methods=["GET"])
@_require_auth
def metrics_json():
    t0 = time.time()
    payload = {**base_status(), "build": build_info(), "metrics": {"enabled": bool(_has_prom)}}
    code = 200
    elapsed = time.time() - t0
    record_metrics("/metrics", "GET", code, elapsed)
    return json_response(payload, code)

@app.route("/status", methods=["GET"])
@_require_auth
def status():
    t0 = time.time()
    out = {**base_status(), **build_info(), "metrics": {"enabled": bool(_has_prom)}}
    elapsed = time.time() - t0
    record_metrics("/status", "GET", 200, elapsed)
    return json_response(out, 200)

@app.route("/debug", methods=["GET"])
@_require_auth
def debug_page():
    """Human-friendly HTML dashboard."""
    info = {**base_status(), **build_info()}
    html = f"""
    <html>
    <head>
        <title>{SERVICE_NAME} – Debug</title>
        <style>
            body {{
                font-family: system-ui, sans-serif;
                background-color: #f8f9fa;
                color: #333;
                margin: 2em;
            }}
            h1 {{ color: #1a73e8; }}
            table {{
                border-collapse: collapse;
                margin-top: 1em;
                width: 100%;
                max-width: 700px;
            }}
            th, td {{
                border: 1px solid #ccc;
                padding: 0.6em 1em;
                text-align: left;
            }}
            th {{
                background-color: #e9ecef;
                text-transform: uppercase;
                font-size: 0.8em;
            }}
            .ok {{ color: #34a853; font-weight: bold; }}
            .fail {{ color: #ea4335; font-weight: bold; }}
            .tip {{ margin-top: 1em; font-size: 0.9em; color: #555; }}
            code {{ background:#eef; padding:2px 4px; border-radius:4px; }}
        </style>
    </head>
    <body>
        <h1>{SERVICE_NAME} Debug Dashboard</h1>
        <p><strong>Status:</strong> <span class="{ 'ok' if info['status']=='ok' else 'fail' }">{info['status']}</span></p>
        <table>
            <tr><th>Mode</th><td>{info['mode']}</td></tr>
            <tr><th>Uptime (sec)</th><td>{info['uptime_seconds']}</td></tr>
            <tr><th>Twilio Ready</th><td>{info['twilio_ready']}</td></tr>
            <tr><th>Region</th><td>{info['region']}</td></tr>
            <tr><th>Build ID</th><td>{info['build_id']}</td></tr>
            <tr><th>Git Commit</th><td>{info['git_commit']}</td></tr>
            <tr><th>Version</th><td>{info['version']}</td></tr>
            <tr><th>Build Time</th><td>{info['build_time']}</td></tr>
        </table>
        <p class="tip">
            Tip: You can also use a query token if configured:
            <code>/debug?token=YOUR_TOKEN</code>
        </p>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")

@app.route("/send-sms", methods=["POST"])
@_require_auth   # protect SMS until A2P is live (remove if you want it public)
def send_sms():
    t0 = time.time()
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or "").strip()
    body = (data.get("body") or "").strip()
    if not to or not body:
        record_error("/send-sms")
        return json_response({"status": "error", "message": "to and body are required"}, 400)
    if MODE == "twilio" and twilio_client:
        try:
            msg = twilio_client.messages.create(to=to, messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID, body=body)
            code = 200
            resp = {"status": "sent", "sid": msg.sid}
        except Exception as e:
            record_error("/send-sms")
            code = 502
            resp = {"status": "twilio_error", "message": str(e)}
    else:
        code = 200
        resp = {"status": "mock_sent", "to": to, "body": body}
    elapsed = time.time() - t0
    record_metrics("/send-sms", "POST", code, elapsed)
    return json_response(resp, code)

# ---------- main ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    log.info("Starting %s on 0.0.0.0:%s (local dev).", SERVICE_NAME, port)
    app.run(host="0.0.0.0", port=port)