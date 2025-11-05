import os
import time
import uuid
import logging
from flask import Flask, request, jsonify, g

# --- Optional Twilio import (safe to run without Twilio) ---
try:
    from twilio.rest import Client
except Exception:
    Client = None

app = Flask(__name__)

# --- Logging: structured, single-line per event ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s | %(levelname)s | %(message)s',
)
logger = logging.getLogger("homehealth")

START_TIME = time.time()

# --- Config / Env (no secrets printed) ---
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID", "") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN", "") or "").strip()
TWILIO_MESSAGING_SERVICE_SID = (os.getenv("TWILIO_MESSAGING_SERVICE_SID", "") or "").strip()

TWILIO_READY = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID, Client is not None])

twilio_client = None
if TWILIO_READY:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception as e:
        logger.warning(f"Twilio init failed, falling back to mock mode: {e}")
        TWILIO_READY = False
        twilio_client = None

def redact(value: str) -> str:
    """Redact secrets for logs: keep last 4 if long enough."""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:2]}…{value[-4:]}"

# --- Request ID middleware + response header ---
@app.before_request
def add_request_id():
    g.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    # minimal structured log per request
    logger.info(f'event=request_start path="{request.path}" method="{request.method}" rid="{g.request_id}"')

@app.after_request
def inject_headers(resp):
    rid = getattr(g, "request_id", None)
    if rid:
        resp.headers["X-Request-ID"] = rid
    resp.headers["Cache-Control"] = "no-store"
    return resp

# --- JSON error handler ---
@app.errorhandler(Exception)
def handle_exception(e):
    logger.exception(f'event=exception rid="{getattr(g, "request_id", "-")}" msg="{e}"')
    return jsonify({
        "error": "internal_server_error",
        "message": str(e),
        "request_id": getattr(g, "request_id", None)
    }), 500

# --- Routes ---
@app.route("/", methods=["GET"])
def root():
    return "Home Health Assistant API (Cloud) ✅"

@app.route("/healthz", methods=["GET"])
def healthz():
    mode = "twilio" if TWILIO_READY else "mock"
    uptime = round(time.time() - START_TIME, 2)
    return jsonify({
        "service": "Home Health Assistant",
        "status": "ok",
        "mode": mode,
        "twilio_ready": TWILIO_READY,
        "uptime_seconds": uptime
    })

@app.route("/version", methods=["GET"])
def version():
    # Render injects these; locally they’ll be "local"
    return jsonify({
        "service": "Home Health Assistant",
        "git_commit": os.getenv("RENDER_GIT_COMMIT", "local"),
        "build_id": os.getenv("RENDER_BUILD_ID", "local"),
        "deploy_id": os.getenv("RENDER_DEPLOY_ID", "local"),
        "python": os.getenv("PYTHON_VERSION", "system"),
        "env": os.getenv("FLASK_ENV", "production")
    })

@app.route("/debug/ping", methods=["GET"])
def debug_ping():
    return jsonify({
        "pong": True,
        "rid": getattr(g, "request_id", None)
    })

@app.route("/debug/echo", methods=["POST"])
def debug_echo():
    payload = None
    try:
        payload = request.get_json(silent=True)
    except Exception:
        payload = None
    return jsonify({
        "echo": payload,
        "headers": {k: v for k, v in request.headers.items()},
        "rid": getattr(g, "request_id", None)
    })

@app.route("/send-sms", methods=["POST"])
def send_sms():
    data = request.get_json(force=True, silent=True) or {}
    to = (data.get("to") or "").strip()
    body = data.get("body") or ""

    if not to or not body:
        return jsonify({"error": "missing_to_or_body"}), 400

    # If Twilio creds are present we send for real; otherwise we mock.
    if TWILIO_READY and twilio_client:
        try:
            msg = twilio_client.messages.create(
                to=to,
                messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
                body=body
            )
            logger.info(f'event=sms_sent to="{to}" sid="{msg.sid}" rid="{getattr(g, "request_id", None)}"')
            return jsonify({"status": "sent", "sid": msg.sid, "to": to})
        except Exception as e:
            logger.exception(f'event=sms_error to="{to}" error="{e}"')
            return jsonify({"status": "twilio_error", "message": str(e)}), 502

    # Mock path (safe during A2P setup or in dev)
    logger.info(f'event=sms_mock to="{to}" rid="{getattr(g, "request_id", None)}"')
    return jsonify({"status": "mocked", "to": to, "body": body})

# --- Startup log (safe, redacted) ---
def _startup_banner():
    mode = "twilio" if TWILIO_READY else "mock"
    logger.info("✅ Home Health Assistant API started")
    logger.info(
        f"mode={mode} twilio_sid={redact(TWILIO_ACCOUNT_SID)} "
        f"msg_service={redact(TWILIO_MESSAGING_SERVICE_SID)} "
        f"git_commit={os.getenv('RENDER_GIT_COMMIT', 'local')}"
    )

_startup_banner()

# Local run (Render uses gunicorn via `app:app`)
if __name__ == "__main__":
    # Use PORT if provided (Render sets PORT at runtime; local defaults to 5000)
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)