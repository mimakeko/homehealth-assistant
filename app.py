from flask import Flask, request, jsonify
import os

# If you want to actually hit Twilio:
from twilio.rest import Client

app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "Home Health Assistant API (Cloud) ✅", 200

@app.route("/send-sms", methods=["POST"])
def send_sms():
    data = request.get_json(force=True)

    to_number = data.get("to")
    body = data.get("body", "")

    if not to_number:
        return jsonify({"error": "Missing 'to'"}), 400

    # decide if we really call Twilio or just mock
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID")

    # if any of these is missing, just mock it
    if not (twilio_sid and twilio_token and messaging_service_sid):
        return jsonify({
            "status": "mocked",
            "to": to_number,
            "body": body
        }), 200

    client = Client(twilio_sid, twilio_token)

    try:
        msg = client.messages.create(
            to=to_number,
            messaging_service_sid=messaging_service_sid,
            body=body
        )
        return jsonify({
            "status": "sent-to-twilio",
            "sid": msg.sid,
            "to": to_number,
            "body": body
        }), 200
    except Exception as e:
        # you may still hit the 10DLC error here – that’s OK right now
        return jsonify({
            "status": "twilio-error",
            "error": str(e)
        }), 500

@app.route("/sms-webhook", methods=["POST"])
def sms_webhook():
    """
    This is the route Twilio will POST to when a patient replies.
    For now we just log and echo back.
    """
    from_number = request.form.get("From")
    body = request.form.get("Body")

    print(f"📨 Incoming SMS from {from_number}: {body}")

    # you can respond with TwiML or just 200 OK
    return "OK", 200


if __name__ == "__main__":
    # Render sets PORT in env. Locally it will default to 5000.
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)