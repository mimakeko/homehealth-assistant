import os
import logging
import json
import uuid
from datetime import datetime
from flask import Flask, request, jsonify

# --- Logging setup (JSON-ish lines, good for Render logs) ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("homehealth")

app = Flask(__name__)

# --- Config / Env ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()

# Optional local dev phone override (for quick tests)
DEFAULT_TO = os.getenv("DEFAULT_TO", "").strip()

# Decide if we are allowed to hit Twilio or run in mock mode
TWILIO_READY = all([
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_MESSAGING_SERVICE_SID
])

twilio_client = None
if TWILIO_READY:
    try:
        from twilio.rest import Client  # import only if creds exist
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        log.info("Twilio client initialized.")
    except Exception as e:
        log.warning(f"Twilio import/init failed; falling back to mock. err={e}")
        twilio_client = None

def req_id():
    """Simple request-id for tracing."""
    return request.headers.get("X-Request-ID") or str(uuid.uuid4())

def respond(payload: dict, status=200):
    """Uniform JSON responses with tracing & service metadata."""
    body = {
        "request_id": req_id(),
        "service": "Home Health Assistant",
        "mode": "twilio" if TWILIO_READY and twilio_client else "mock",
        "twilio_ready": bool(TWILIO_READY and twilio_client),
        "ts": datetime.utcnow().isoformat() + "Z",
        **payload,
    }
    return jsonify(body), status

# --- Routes ---

@app.route("/", methods=["GET"])
def root():
    return respond({"status": "ok", "message": "Home Health Assistant API (Cloud) ✅"})

@app.route("/healthz", methods=["GET"])
def healthz():
    return respond({"status": "ok"})

@app.route("/version", methods=["GET"])
def version():
    return respond({
        "status": "ok",
        "version": os.getenv("APP_VERSION", "v1.0.0"),
        "python": os.getenv("PYTHON_VERSION", ""),
        "env": os.getenv("FLASK_ENV", ""),
    })

@app.route("/debug/env", methods=["GET"])
def debug_env():
    # Safe peek (never show secrets!)
    safe = {
        "FLASK_ENV": os.getenv("FLASK_ENV", ""),
        "PYTHON_VERSION": os.getenv("PYTHON_VERSION", ""),
        "TWILIO_ACCOUNT_SID_present": bool(TWILIO_ACCOUNT_SID),
        "TWILIO_AUTH_TOKEN_present": bool(TWILIO_AUTH_TOKEN),
        "TWILIO_MESSAGING_SERVICE_SID_present": bool(TWILIO_MESSAGING_SERVICE_SID),
        "DEFAULT_TO_present": bool(DEFAULT_TO),
        "LOG_LEVEL": LOG_LEVEL,
    }
    return respond({"status": "ok", "env_preview": safe})

@app.route("/send-sms", methods=["POST"])
def send_sms():
    """
    Body:
      { "to": "+1xxxxxxxxxx", "body": "hello" }
    If TWILIO_READY=false -> returns mocked response.
    """
    rid = req_id()
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or DEFAULT_TO or "").strip()
    body = (data.get("body") or "").strip()

    if not to or not body:
        return respond({"status": "error", "error": "Missing 'to' or 'body'."}, status=400)

    log.info(json.dumps({
        "event": "send-sms.attempt",
        "request_id": rid,
        "to": to[-4:],  # last 4 only
        "body_len": len(body),
        "twilio_ready": bool(TWILIO_READY and twilio_client),
    }))

    if not (TWILIO_READY and twilio_client):
        # Mocked path (safe before A2P approval)
        return respond({"status": "mocked", "to": to, "body": body})

    # Real Twilio path
    try:
        msg = twilio_client.messages.create(
            messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
            to=to,
            body=body
        )
        log.info(json.dumps({
            "event": "send-sms.sent",
            "request_id": rid,
            "sid": msg.sid,
            "status": getattr(msg, "status", "unknown"),
        }))
        return respond({
            "status": "sent",
            "sid": msg.sid,
            "to": to,
            "body_len": len(body),
        })
    except Exception as e:
        log.error(json.dumps({
            "event": "send-sms.error",
            "request_id": rid,
            "error": str(e),
        }))
        return respond({"status": "twilio-error", "error": str(e)}, status=502)

# Only for local dev (“python app.py”). Render uses gunicorn.
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    log.info(f"Starting Flask on 0.0.0.0:{port} (local dev)…")
    app.run(host="0.0.0.0", port=port)