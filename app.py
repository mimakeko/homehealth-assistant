import os
from flask import Flask, request, jsonify

# --- Optional Twilio import (safe if library isn't installed yet) ---
try:
    from twilio.rest import Client
except Exception:
    Client = None  # library not present; stay in mock mode

app = Flask(__name__)

# --- Config / Env ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()

# If any Twilio secret is missing or the library isn't installed, we operate in mock mode
TWILIO_READY = all([
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_MESSAGING_SERVICE_SID
]) and (Client is not None)

twilio_client = None
if TWILIO_READY and Client is not None:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# >>> New helpful boot message for logs <<<
print("✅ Home Health Assistant API started successfully in",
      "mock" if not TWILIO_READY else "live", "mode")

# --- Routes ---

@app.route("/", methods=["GET"])
def root():
    return "Home Health Assistant API (Cloud) ✅"

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "service": "Home Health Assistant",
        "status": "ok",
        "mode": "mock" if not TWILIO_READY else "live",
        "twilio_ready": TWILIO_READY
    }), 200

@app.route("/send-sms", methods=["POST"])
def send_sms():
    data = request.get_json(silent=True) or {}
    to = data.get("to")
    body = data.get("body", "")

    if not to:
        return jsonify({"error": "Missing 'to'"}), 400
    if not body:
        return jsonify({"error": "Missing 'body'"}), 400

    # Mock mode (safe before A2P approval or without creds)
    if not TWILIO_READY or twilio_client is None:
        return jsonify({"status": "mocked", "to": to, "body": body}), 200

    # Live mode
    try:
        msg = twilio_client.messages.create(
            to=to,
            messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
            body=body
        )
        return jsonify({"status": "sent", "sid": msg.sid}), 200
    except Exception as e:
        return jsonify({"status": "twilio-error", "error": str(e)}), 502

if __name__ == "__main__":
    # Local dev server (Render uses gunicorn per your Start Command)
    app.run(host="0.0.0.0", port=5000, debug=False)