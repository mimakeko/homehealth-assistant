import os
from flask import Flask, request, jsonify

# Twilio is optional at runtime; we'll only use it if creds exist
try:
    from twilio.rest import Client  # type: ignore
except Exception:
    Client = None  # library not strictly required for mock mode

app = Flask(__name__)

# --- Config / Env ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()

# If any Twilio secret is missing, we operate in mock mode (safe for A2P-unapproved phase)
TWILIO_READY = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID])

twilio_client = None
if TWILIO_READY and Client is not None:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- Routes ---

@app.route("/", methods=["GET"])
def root():
    return "Home Health Assistant API (Cloud) ✅"

@app.route("/healthz", methods=["GET"])
def healthz():
    """
    Lightweight health check for Render / uptime monitors.
    """
    return jsonify(
        status="ok",
        service="Home Health Assistant",
        twilio_ready=bool(TWILIO_READY),
        mode="live" if TWILIO_READY else "mock"
    ), 200

@app.route("/send-sms", methods=["POST"])
def send_sms():
    """
    Body:
    {
      "to": "+1XXXXXXXXXX",
      "body": "Message text"
    }
    """
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or "").strip()
    body = (data.get("body") or "").strip()

    if not to or not body:
        return jsonify(error="Both 'to' and 'body' are required."), 400

    # Mock mode (safe while A2P/10DLC campaign is pending)
    if not TWILIO_READY or twilio_client is None:
        return jsonify(
            status="mocked",
            to=to,
            body=body,
            note="Twilio not active; returning simulated success."
        ), 200

    # Live mode via Messaging Service
    try:
        msg = twilio_client.messages.create(
            messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
            to=to,
            body=body,
        )
        return jsonify(
            status="sent",
            sid=msg.sid,
            to=to,
            body=body
        ), 200
    except Exception as e:
        # Don’t leak secrets; return safe error
        return jsonify(status="twilio-error", error=str(e)[:400]), 502

@app.route("/twilio-webhook", methods=["POST"])
def twilio_webhook():
    """
    Twilio will POST inbound replies here (after you wire the webhook in the console).
    For now we just acknowledge; later we’ll parse and route to therapists.
    """
    # You can inspect request.form for 'From', 'Body', etc.
    # Example minimal OK:
    return ("", 204)

# Render/Heroku style entrypoint
# (gunicorn uses: `gunicorn app:app`)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))