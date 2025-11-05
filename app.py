# app.py
import os
import time
import json
import threading
from datetime import datetime, timezone
from typing import Dict, Any

from flask import Flask, request, jsonify, Response

# --- Optional Twilio import (safe if not installed/needed) ---
try:
    from twilio.rest import Client as TwilioClient  # type: ignore
except Exception:
    TwilioClient = None  # library not installed — stays in mock mode

app = Flask(__name__)

START_TS = time.time()
SERVICE_NAME = "Home Health Assistant"

# ----------------------------
# Environment & Twilio wiring
# ----------------------------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()

TWILIO_READY = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID]) and TwilioClient is not None
twilio_client = None
if TWILIO_READY:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        # If credentials are wrong, fall back to mock mode
        TWILIO_READY = False
        twilio_client = None

# ----------------------------
# Build / version fingerprint
# ----------------------------
def _git_commit() -> str:
    # Best effort, safe in read-only cloud
    try:
        head = os.popen("git rev-parse --short=10 HEAD 2>/dev/null").read().strip()
        return head or "unknown"
    except Exception:
        return "unknown"

BUILD_INFO = {
    "build_id": os.getenv("RENDER_BUILD_ID", "local"),
    "region": os.getenv("RENDER_REGION", "local"),
    "git_commit": _git_commit(),
    "service": SERVICE_NAME,
}

# ----------------------------
# Simple metrics (thread-safe)
# ----------------------------
_metrics_lock = threading.Lock()
REQ_COUNT: Dict[str, int] = {}
ERR_COUNT: Dict[str, int] = {}
LAT_SUM: Dict[str, int] = {}      # microseconds
LAT_COUNT: Dict[str, int] = {}

def _avg_latency(route: str) -> float:
    # FIX: prevent division-by-zero on fresh deploys
    with _metrics_lock:
        total_us = LAT_SUM.get(route, 0)
        n = LAT_COUNT.get(route, 0)
    if n == 0:
        return 0.0
    return (total_us / 1_000_000.0) / n

def _mark_request(route: str, ok: bool, latency_us: int) -> None:
    with _metrics_lock:
        REQ_COUNT[route] = REQ_COUNT.get(route, 0) + 1
        if not ok:
            ERR_COUNT[route] = ERR_COUNT.get(route, 0) + 1
        LAT_SUM[route] = LAT_SUM.get(route, 0) + latency_us
        LAT_COUNT[route] = LAT_COUNT.get(route, 0) + 1

# ---------------
# Helpers
# ---------------
def _uptime_seconds() -> float:
    return round(time.time() - START_TS, 2)

def _json_response(payload: Dict[str, Any], status: int = 200) -> Response:
    # structured JSON everywhere
    return Response(json.dumps(payload, ensure_ascii=False), status=status, mimetype="application/json")

def _service_mode() -> str:
    return "twilio" if TWILIO_READY else "mock"

# ---------------
# Routes
# ---------------
@app.route("/", methods=["GET"])
def root():
    t0 = time.perf_counter_ns()
    payload = {"service": SERVICE_NAME, "status": "ok", "mode": _service_mode()}
    resp = _json_response(payload)
    _mark_request("/", True, int((time.perf_counter_ns() - t0) / 1000))
    return resp

@app.route("/healthz", methods=["GET"])
def healthz():
    t0 = time.perf_counter_ns()
    payload = {
        "mode": _service_mode(),
        "service": SERVICE_NAME,
        "status": "ok",
        "twilio_ready": TWILIO_READY,
        "uptime_seconds": _uptime_seconds(),
    }
    resp = _json_response(payload)
    _mark_request("/healthz", True, int((time.perf_counter_ns() - t0) / 1000))
    return resp

@app.route("/version", methods=["GET"])
def version():
    t0 = time.perf_counter_ns()
    payload = {
        **BUILD_INFO,
        "python_version": os.getenv("PYTHON_VERSION", ""),
        "flask_env": os.getenv("FLASK_ENV", ""),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    resp = _json_response(payload)
    _mark_request("/version", True, int((time.perf_counter_ns() - t0) / 1000))
    return resp

@app.route("/send-sms", methods=["POST"])
def send_sms():
    t0 = time.perf_counter_ns()
    route = "/send-sms"

    try:
        data = request.get_json(silent=True) or {}
        to = (data.get("to") or "").strip()
        body = (data.get("body") or "").strip()

        if not to or not body:
            raise ValueError("Both 'to' and 'body' are required.")

        if TWILIO_READY and twilio_client:
            try:
                msg = twilio_client.messages.create(
                    to=to,
                    messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
                    body=body,
                )
                resp = _json_response({
                    "status": "sent",
                    "mode": "twilio",
                    "sid": msg.sid,
                    "to": to,
                    "body": body,
                }, status=200)
                _mark_request(route, True, int((time.perf_counter_ns() - t0) / 1000))
                return resp
            except Exception as e:
                # fall back to a clear error
                resp = _json_response({
                    "status": "twilio_error",
                    "mode": "twilio",
                    "error": str(e),
                    "to": to,
                }, status=502)
                _mark_request(route, False, int((time.perf_counter_ns() - t0) / 1000))
                return resp

        # Mock path (safe before A2P approval)
        resp = _json_response({
            "status": "mocked",
            "mode": "mock",
            "to": to,
            "body": body,
        }, status=200)
        _mark_request(route, True, int((time.perf_counter_ns() - t0) / 1000))
        return resp

    except Exception as e:
        resp = _json_response({
            "status": "error",
            "mode": _service_mode(),
            "error": str(e),
        }, status=400)
        _mark_request(route, False, int((time.perf_counter_ns() - t0) / 1000))
        return resp

@app.route("/metrics", methods=["GET"])
def metrics_json():
    t0 = time.perf_counter_ns()
    # snapshot under lock
    with _metrics_lock:
        routes = list({*REQ_COUNT.keys(), *ERR_COUNT.keys(), *LAT_COUNT.keys()})

    metrics = {
        "status": "ok",
        "service": SERVICE_NAME,
        "mode": _service_mode(),
        "uptime_seconds": _uptime_seconds(),
        "routes": {},
        "sms": {
            "twilio_ready": TWILIO_READY,
        },
        "build": BUILD_INFO,
    }

    for r in routes:
        metrics["routes"][r] = {
            "requests": REQ_COUNT.get(r, 0),
            "errors": ERR_COUNT.get(r, 0),
            "avg_latency_seconds": round(_avg_latency(r), 6),
        }

    resp = _json_response(metrics)
    _mark_request("/metrics", True, int((time.perf_counter_ns() - t0) / 1000))
    return resp

@app.route("/metrics.prom", methods=["GET"])
def metrics_prom():
    t0 = time.perf_counter_ns()
    # A tiny Prometheus exposition; safe when counts are zero
    lines = [
        f'# HELP hha_uptime_seconds Service uptime in seconds',
        f'# TYPE hha_uptime_seconds gauge',
        f'hha_uptime_seconds {_uptime_seconds()}',
        '',
        '# HELP hha_route_requests_total Requests per route',
        '# TYPE hha_route_requests_total counter',
    ]
    with _metrics_lock:
        for r, c in REQ_COUNT.items():
            lines.append(f'hha_route_requests_total{{route="{r}"}} {c}')

        lines += ['', '# HELP hha_route_errors_total Errors per route', '# TYPE hha_route_errors_total counter']
        for r, c in ERR_COUNT.items():
            lines.append(f'hha_route_errors_total{{route="{r}"}} {c}')

        lines += ['', '# HELP hha_route_avg_latency_seconds Average latency per route', '# TYPE hha_route_avg_latency_seconds gauge']
        for r in set(list(REQ_COUNT.keys()) + list(LAT_COUNT.keys())):
            lines.append(f'hha_route_avg_latency_seconds{{route="{r}"}} {round(_avg_latency(r), 6)}')

    out = "\n".join(lines) + "\n"
    resp = Response(out, status=200, mimetype="text/plain; version=0.0.4")
    _mark_request("/metrics.prom", True, int((time.perf_counter_ns() - t0) / 1000))
    return resp

# ----------------------------
# Optional startup self-test
# ----------------------------
def _startup_selftest():
    # Run after Flask is ready; only if enabled
    if os.getenv("STARTUP_SELFTEST", "0") != "1":
        return
    try:
        # cheap, internal calls (no network) via test_client
        with app.test_client() as c:
            c.get("/healthz")
            c.get("/metrics")
    except Exception:
        # never crash on boot if self-test fails
        pass

# Gunicorn/Render entrypoint
if __name__ == "__main__":
    _startup_selftest()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
else:
    # When run by gunicorn, still do the selftest
    _startup_selftest()