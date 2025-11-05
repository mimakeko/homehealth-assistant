import os
import time
import logging
import threading
from collections import Counter
from flask import Flask, request, jsonify

# --- Optional Twilio import (app still runs without it) ---
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None  # library not installed or unavailable

# --- App & logging ---
app = Flask(__name__)
START_TIME = time.time()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("homehealth")

# --- Metrics (thread-safe) ---
_metrics_lock = threading.Lock()
REQ_COUNTER = Counter()          # per-route requests
REQ_ERRORS = Counter()           # per-route errors
LAT_SUM = Counter()              # per-route total latency (seconds * 1e6 to avoid float races)
LAT_COUNT = Counter()            # per-route observed count
SMS_COUNTER = Counter()          # sent, mocked, errors

def _record_latency(route: str, seconds: float):
    micros = int(seconds * 1_000_000)
    with _metrics_lock:
        LAT_SUM[route] += micros
        LAT_COUNT[route] += 1

def _inc(counter: Counter, key: str, n: int = 1):
    with _metrics_lock:
        counter[key] += n

def uptime_seconds() -> float:
    return round(time.time() - START_TIME, 2)

def _avg_latency(route: str) -> float:
    with _metrics_lock:
        total = LAT_SUM[route]
        cnt = LAT_COUNT[route]
    return (total / 1_000_000.0 / cnt) if cnt else 0.0

# --- Helpers ---
def respond(payload: dict, status: int = 200):
    payload = {
        **payload,
        "service": "Home Health Assistant",
    }
    return jsonify(payload), status

def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}

# --- Config / Env ---
FLASK_ENV = os.getenv("FLASK_ENV", "").strip()
PYTHON_VERSION = os.getenv("PYTHON_VERSION", "").strip()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
DEFAULT_TO = os.getenv("DEFAULT_TO", "").strip()  # optional default recipient

# Render/Git metadata if present
RENDER_SERVICE_NAME = os.getenv("RENDER_SERVICE_NAME", "")
RENDER_GIT_COMMIT = os.getenv("RENDER_GIT_COMMIT", "")
RENDER_DEPLOY_ID = os.getenv("RENDER_DEPLOY_ID", "")

TWILIO_READY = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID])
MODE = "live" if (TWILIO_READY and TwilioClient) else "mock"

# Twilio client (only if ready)
twilio_client = None
if MODE == "live":
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        log.info("Twilio client initialized.")
    except Exception:
        log.exception("Failed to initialize Twilio client; falling back to mock.")
        twilio_client = None
        MODE = "mock"

# --- Request hooks (for logs + metrics) ---
@app.before_request
def _log_request():
    request._t0 = time.perf_counter()
    path = request.path
    log.info("REQ %s %s", request.method, path)
    _inc(REQ_COUNTER, path)

@app.after_request
def _after(resp):
    t1 = time.perf_counter()
    path = getattr(request, "path", "unknown")
    _record_latency(path, t1 - getattr(request, "_t0", t1))
    return resp

# --- Routes ---
@app.route("/", methods=["GET"])
def root():
    return "Home Health Assistant API (Cloud) ✅"

@app.route("/healthz", methods=["GET"])
def healthz():
    return respond(
        {
            "mode": MODE,
            "status": "ok",
            "twilio_ready": bool(twilio_client),
            "uptime_seconds": uptime_seconds(),
        }
    )

@app.route("/version", methods=["GET"])
def version():
    return respond(
        {
            "build_id": RENDER_DEPLOY_ID or "local",
            "deploy_id": RENDER_DEPLOY_ID or "local",
            "git_commit": RENDER_GIT_COMMIT or "local",
            "service_name": RENDER_SERVICE_NAME or "homehealth",
        }
    )

@app.route("/debug/env", methods=["GET"])
def debug_env():
    """Safe environment preview (booleans only; never echo secrets)."""
    safe = {
        "FLASK_ENV": FLASK_ENV,
        "PYTHON_VERSION": PYTHON_VERSION,
        "TWILIO_ACCOUNT_SID_present": bool(TWILIO_ACCOUNT_SID),
        "TWILIO_AUTH_TOKEN_present": bool(TWILIO_AUTH_TOKEN),
        "TWILIO_MESSAGING_SERVICE_SID_present": bool(TWILIO_MESSAGING_SERVICE_SID),
        "DEFAULT_TO_present": bool(DEFAULT_TO),
        "LOG_LEVEL": LOG_LEVEL,
        "render": {
            "service": bool(RENDER_SERVICE_NAME),
            "git_commit_present": bool(RENDER_GIT_COMMIT),
            "deploy_id_present": bool(RENDER_DEPLOY_ID),
        },
    }
    return respond({"status": "ok", "env_preview": safe})

@app.route("/send-sms", methods=["POST"])
def send_sms():
    """Send SMS using Twilio when configured; otherwise return mocked result."""
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or DEFAULT_TO or "").strip()
    body = (data.get("body") or "Hello from cloud").strip()

    if not to:
        _inc(REQ_ERRORS, request.path)
        return respond({"status": "error", "message": "Missing 'to' and no DEFAULT_TO set."}, 400)

    if not twilio_client:
        _inc(SMS_COUNTER, "mocked")
        return respond({"status": "mocked", "to": to, "body": body})

    try:
        msg = twilio_client.messages.create(
            to=to,
            messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
            body=body,
        )
        _inc(SMS_COUNTER, "sent")
        return respond({"status": "sent-to-twilio", "to": to, "sid": msg.sid})
    except Exception as e:
        log.exception("Twilio send failed.")
        _inc(SMS_COUNTER, "errors")
        _inc(REQ_ERRORS, request.path)
        return respond({"status": "twilio-error", "to": to, "error": str(e)}, 500)

# --- Metrics endpoints ---
@app.route("/metrics", methods=["GET"])
def metrics_json():
    with _metrics_lock:
        per_route = {
            route: {
                "requests": REQ_COUNTER[route],
                "errors": REQ_ERRORS[route],
                "avg_latency_seconds": round(_avg_latency(route), 6),
            }
            for route in set(list(REQ_COUNTER.keys()) + list(LAT_COUNT.keys()))
        }
        sms = dict(SMS_COUNTER)
    return respond(
        {
            "status": "ok",
            "uptime_seconds": uptime_seconds(),
            "mode": MODE,
            "twilio_ready": bool(twilio_client),
            "routes": per_route,
            "sms": sms,
        }
    )

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    # Minimal Prometheus exposition
    lines = []
    with _metrics_lock:
        lines.append(f'homehealth_uptime_seconds {uptime_seconds()}')
        lines.append(f'homehealth_twilio_ready {{mode="{MODE}"}} {1 if twilio_client else 0}')
        # Per-route
        for route in set(list(REQ_COUNTER.keys()) + list(LAT_COUNT.keys())):
            reqs = REQ_COUNTER[route]
            errs = REQ_ERRORS[route]
            avg = _avg_latency(route)
            r = route.replace('"', '\\"')
            lines.append(f'homehealth_requests_total{{route="{r}"}} {reqs}')
            lines.append(f'homehealth_request_errors_total{{route="{r}"}} {errs}')
            lines.append(f'homehealth_request_avg_latency_seconds{{route="{r}"}} {avg}')
        # SMS
        for k, v in SMS_COUNTER.items():
            lines.append(f'homehealth_sms_total{{status="{k}"}} {v}')
    return ("\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; charset=utf-8"})

# --- Error handling ---
@app.errorhandler(404)
def not_found(_):
    _inc(REQ_ERRORS, request.path if request else "unknown")
    return respond({"error": "not_found", "message": "Route does not exist"}, 404)

@app.errorhandler(Exception)
def unhandled(err):
    log.exception("Unhandled error: %s", err)
    _inc(REQ_ERRORS, request.path if request else "unknown")
    return respond({"error": "internal_server_error", "message": str(err)}, 500)

# --- Local run (Render uses gunicorn `app:app`) ---
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)