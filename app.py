import os, re, json, time, csv, io, sqlite3, math, urllib.parse, urllib.request
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, Response, abort, make_response, render_template_string

# ─────────────────────────────────────────────────────────────────────────────
# Optional libs (Twilio, dateparser). App remains functional without them.
Client = RequestValidator = MessagingResponse = None
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator as TwilioRequestValidator
    from twilio.twiml.messaging_response import MessagingResponse as TwilioMessagingResponse
    Client, RequestValidator, MessagingResponse = TwilioClient, TwilioRequestValidator, TwilioMessagingResponse
except Exception:
    pass

dateparser = None
try:
    import dateparser as _dp
    dateparser = _dp
except Exception:
    pass

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Env / Config
SERVICE_NAME = "Home Health Assistant"
TIMEZONE     = os.getenv("APP_TZ", "America/Chicago")
TZ           = ZoneInfo(TIMEZONE)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_MSS_SID     = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
DEBUG_TOKEN        = os.getenv("DEBUG_TOKEN", "").strip()

# Feature flags (safe defaults)
FEATURE_UI_SCHEDULE     = os.getenv("FEATURE_UI_SCHEDULE", "1") == "1"
FEATURE_DATEPARSER      = bool(dateparser)  # auto if library present
GOOGLE_MAPS_API_KEY     = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
FEATURE_DRIVETIME_ROUTE = bool(GOOGLE_MAPS_API_KEY)  # auto if key present

MODE = "mock"
TWILIO_READY = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_MSS_SID and Client)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_READY else None
validator     = RequestValidator(TWILIO_AUTH_TOKEN) if (TWILIO_READY and RequestValidator) else None
if TWILIO_READY:
    MODE = "live"

START_TS = time.time()
VERSION  = "1.3.0"

# ─────────────────────────────────────────────────────────────────────────────
# Storage: SQLite (messages + patients + appointments)
DB_PATH = "data.sqlite"

def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def db_init():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts REAL, direction TEXT, kind TEXT, intent TEXT,
          frm TEXT, to_number TEXT, body TEXT, note TEXT, sid TEXT
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg_intent ON messages(intent)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg_frm ON messages(frm)")

        con.execute("""
        CREATE TABLE IF NOT EXISTS patients(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT, phone TEXT UNIQUE, address TEXT, city TEXT, state TEXT, zip TEXT,
          lat REAL, lon REAL, therapist TEXT, notes TEXT
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_pat_phone ON patients(phone)")

        con.execute("""
        CREATE TABLE IF NOT EXISTS appointments(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          patient_id INTEGER, therapist TEXT, start_ts REAL,
          duration_min INTEGER, status TEXT, source TEXT, note TEXT,
          FOREIGN KEY(patient_id) REFERENCES patients(id)
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_appt_start ON appointments(start_ts)")
db_init()

# One-time migration from legacy JSON (if file exists and DB empty)
def migrate_from_legacy_json():
    if not os.path.exists("store.json"):
        return
    try:
        d = json.load(open("store.json","r",encoding="utf-8"))
    except Exception:
        return
    msgs = d.get("messages", [])
    if not msgs: return
    with db() as con:
        if con.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] > 0:
            return
        for m in msgs:
            con.execute("""
             INSERT INTO messages(ts,direction,kind,intent,frm,to_number,body,note,sid)
             VALUES (?,?,?,?,?,?,?,?,?)""",
             (float(m.get("ts", time.time())),
              (m.get("meta",{}) or {}).get("kind") or m.get("kind") or "mock",
              (m.get("meta",{}) or {}).get("kind") or m.get("kind") or "mock",
              (m.get("meta",{}) or {}).get("intent") or m.get("intent") or "other",
              m.get("to") if m.get("direction")=="in" else "",
              m.get("to") if m.get("direction")=="out" else "",
              m.get("body",""), m.get("note",""), (m.get("meta",{}) or {}).get("sid")))
migrate_from_legacy_json()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers & auth
def require_token():
    tok = request.headers.get("X-Debug-Token") or request.args.get("token")
    if not DEBUG_TOKEN or tok != DEBUG_TOKEN:
        abort(401, description="Unauthorized")
    return True

def now_tz():
    return datetime.now(TZ)

def to_epoch(dt: datetime) -> float:
    return dt.timestamp()

def from_epoch(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ)

def log_message(direction, body, frm="", to="", intent="other", kind="mock", note="", sid=None):
    with db() as con:
        con.execute("""INSERT INTO messages(ts,direction,kind,intent,frm,to_number,body,note,sid)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (time.time(), direction, kind, intent, frm, to, body, note, sid))

def find_patient_by_phone(phone: str):
    if not phone: return None
    with db() as con:
        row = con.execute("SELECT * FROM patients WHERE phone = ?", (phone.strip(),)).fetchone()
        return dict(row) if row else None

# ─────────────────────────────────────────────────────────────────────────────
# Intent & time parsing
YES_RE   = re.compile(r"\b(yes|yeah|yep|ok|okay|confirm|confirmed|si|sim)\b", re.I)
RESCH_RE = re.compile(r"\b(resched|reschedule|another time|different time|move|change)\b", re.I)
TIME_RE  = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
WEEKDAY_MAP = {
    "mon":0,"monday":0,"tue":1,"tues":1,"tuesday":1,"wed":2,"weds":2,"wednesday":2,
    "thu":3,"thur":3,"thurs":3,"thursday":3,"fri":4,"friday":4,"sat":5,"saturday":5,"sun":6,"sunday":6
}

def detect_intent(text: str) -> str:
    t = (text or "").lower()
    if YES_RE.search(t): return "confirm"
    if RESCH_RE.search(t): return "reschedule"
    if TIME_RE.search(t): return "time"
    return "other"

def parse_natural_time_minimal(text: str, base: datetime | None = None):
    """Minimal fallback parser."""
    base = base or now_tz()
    t = (text or "").lower()
    target_day = base.date()
    if "tomorrow" in t:
        target_day = (base + timedelta(days=1)).date()
    else:
        for key, idx in WEEKDAY_MAP.items():
            if key in t:
                delta = (idx - base.weekday()) % 7
                if delta == 0 and "next" in t: delta = 7
                target_day = (base + timedelta(days=delta)).date()
                break
    m = TIME_RE.search(t)
    if not m:
        return None, None, "no_time_found"
    hour = int(m.group(1)); minute = int(m.group(2) or "0"); ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12: hour += 12
    if ampm == "am" and hour == 12: hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59): return None, None, "invalid_time"
    start_dt = datetime(target_day.year, target_day.month, target_day.day, hour, minute, tzinfo=TZ)
    duration = 60
    dur = re.search(r"\b(\d+)\s*(min|mins|minutes)\b", t)
    if dur: duration = int(dur.group(1))
    else:
        hrs = re.search(r"\b(\d+(?:\.\d+)?)\s*h(?:ours?)?\b", t)
        if hrs: duration = int(float(hrs.group(1)) * 60)
    return start_dt, duration, "ok"

def parse_natural_time(text: str, base: datetime | None = None):
    """Rich parser if dateparser available; else minimal fallback."""
    if FEATURE_DATEPARSER and dateparser:
        base = base or now_tz()
        dp = dateparser.parse(text, settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": base,
            "TIMEZONE": TIMEZONE,
            "RETURN_AS_TIMEZONE_AWARE": True
        })
        if dp:
            # normalize tz if parser returned naive
            if dp.tzinfo is None: dp = dp.replace(tzinfo=TZ)
            # duration heuristic (same as minimal)
            duration = 60
            t = (text or "").lower()
            dur = re.search(r"\b(\d+)\s*(min|mins|minutes)\b", t)
            if dur: duration = int(dur.group(1))
            else:
                hrs = re.search(r"\b(\d+(?:\.\d+)?)\s*h(?:ours?)?\b", t)
                if hrs: duration = int(float(hrs.group(1)) * 60)
            return dp, duration, "ok"
    return parse_natural_time_minimal(text, base)

# ─────────────────────────────────────────────────────────────────────────────
# Scheduling helpers
def create_or_update_appt(patient_id: int, therapist: str, start_dt: datetime, duration_min=60,
                          status="confirmed", source="inbound", note=""):
    with db() as con:
        day_start = datetime(start_dt.year, start_dt.month, start_dt.day, 0, 0, tzinfo=TZ)
        day_end   = day_start + timedelta(days=1)
        row = con.execute("""
            SELECT * FROM appointments WHERE patient_id=? AND start_ts BETWEEN ? AND ?
            ORDER BY start_ts DESC LIMIT 1
        """, (patient_id, to_epoch(day_start), to_epoch(day_end))).fetchone()
        if row:
            con.execute("""UPDATE appointments
                           SET therapist=?, start_ts=?, duration_min=?, status=?, source=?, note=?
                           WHERE id=?""",
                        (therapist, to_epoch(start_dt), duration_min, status, source, note, row["id"]))
            return row["id"]
        cur = con.execute("""INSERT INTO appointments(patient_id,therapist,start_ts,duration_min,status,source,note)
                             VALUES(?,?,?,?,?,?,?)""",
                          (patient_id, therapist, to_epoch(start_dt), duration_min, status, source, note))
        return cur.lastrowid

def list_appointments(day: date, therapist: str | None = None):
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
    end   = start + timedelta(days=1)
    args = [to_epoch(start), to_epoch(end)]
    sql = """
      SELECT a.*, p.name as patient_name, p.phone as patient_phone,
             p.address, p.city, p.state, p.zip, p.lat, p.lon
        FROM appointments a
   LEFT JOIN patients p ON p.id = a.patient_id
       WHERE start_ts BETWEEN ? AND ?
    """
    if therapist:
        sql += " AND a.therapist=?"; args.append(therapist)
    sql += " ORDER BY start_ts ASC"
    with db() as con:
        rows = con.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    to_rad = math.radians
    dlat = to_rad(lat2 - lat1); dlon = to_rad(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(to_rad(lat1))*math.cos(to_rad(lat2))*math.sin(dlon/2)**2)
    return 2*R*math.atan2(math.sqrt(a), math.sqrt(1-a))  # km

def distance_matrix_minutes(orig, dest):
    """Return minutes using Google Distance Matrix if key present; else None."""
    if not FEATURE_DRIVETIME_ROUTE: return None
    try:
        params = {
            "origins": f"{orig['lat']},{orig['lon']}",
            "destinations": f"{dest['lat']},{dest['lon']}",
            "key": GOOGLE_MAPS_API_KEY,
            "units": "imperial"
        }
        url = "https://maps.googleapis.com/maps/api/distancematrix/json?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        rows = data.get("rows", [])
        if not rows: return None
        el = rows[0].get("elements", [])
        if not el: return None
        dur = el[0].get("duration")
        if not dur: return None
        return dur["value"]/60.0  # seconds -> minutes
    except Exception:
        return None

def optimize_route(appts):
    """Nearest-neighbor by drive time if API is configured; else by distance; falls back to current order."""
    pts = [a for a in appts if a.get("lat") is not None and a.get("lon") is not None]
    if len(pts) < 2:
        return appts
    ordered = [min(pts, key=lambda r: r["start_ts"])]
    remaining = [r for r in pts if r is not ordered[0]]
    while remaining:
        last = ordered[-1]
        best = None; best_score = 1e9
        for r in remaining:
            # Prefer drive time if available
            mins = distance_matrix_minutes(last, r)
            if mins is None:
                # fallback to haversine km
                mins = haversine(last["lat"], last["lon"], r["lat"], r["lon"])
            if mins < best_score:
                best, best_score = r, mins
        ordered.append(best); remaining.remove(best)
    id_to_order = {a["id"]: i for i,a in enumerate(ordered)}
    return sorted(appts, key=lambda r: id_to_order.get(r["id"], 9999))

# ─────────────────────────────────────────────────────────────────────────────
# Public routes (no token)
@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": SERVICE_NAME, "status": "ok", "mode": MODE,
                    "feature_ui": FEATURE_UI_SCHEDULE, "feature_dateparser": FEATURE_DATEPARSER,
                    "feature_drivetime": FEATURE_DRIVETIME_ROUTE})

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "service": SERVICE_NAME, "status": "ok", "mode": MODE, "twilio_ready": TWILIO_READY,
        "uptime_seconds": round(time.time()-START_TS,2), "tz": TIMEZONE, "version": VERSION,
        "features": {"ui_schedule": FEATURE_UI_SCHEDULE, "dateparser": FEATURE_DATEPARSER, "drivetime": FEATURE_DRIVETIME_ROUTE}
    })

@app.route("/simulate-sms", methods=["POST"])
def simulate_sms():
    data = request.get_json(silent=True) or {}
    frm  = (data.get("from") or "").strip()
    body = (data.get("body") or "").strip()
    intent = detect_intent(body)
    log_message("in", body, frm=frm, to="", intent=intent, kind="simulate", note="simulate-in")

    patient = find_patient_by_phone(frm)
    if patient:
        start_dt, dur, _ = parse_natural_time(body)
        if start_dt:
            create_or_update_appt(patient["id"], patient.get("therapist") or "therapist",
                                  start_dt, dur or 60,
                                  status="confirmed" if intent=="confirm" else "pending",
                                  source="inbound", note="auto from simulate")
            thanks = "Thanks! See you at the scheduled time."
            log_message("out", thanks, frm="", to=frm, intent="other", kind="simulate", note="auto-reply")
            return jsonify({"ok": True, "intent": intent, "scheduled_for": start_dt.isoformat(),
                            "duration_min": dur or 60})
    if intent == "confirm":
        thanks = "Thanks! See you at the scheduled time."
        log_message("out", thanks, frm="", to=frm, intent="other", kind="simulate", note="auto-reply")
    return jsonify({"ok": True, "intent": intent})

@app.route("/send-sms", methods=["POST"])
def send_sms():
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or "").strip()
    body = (data.get("body") or "").strip()
    if not to or not body:
        return jsonify({"ok": False, "error": "missing_fields"}), 400
    if TWILIO_READY:
        try:
            callback = request.url_root.rstrip("/") + "/status-callback"
            msg = twilio_client.messages.create(messaging_service_sid=TWILIO_MSS_SID,
                                                to=to, body=body, status_callback=callback)
            log_message("out", body, frm="", to=to, intent="other", kind="twilio", note="live",
                        sid=getattr(msg,"sid",None))
            return jsonify({"ok": True, "sid": getattr(msg,"sid","queued"), "status": "queued"})
        except Exception as e:
            log_message("out", body, frm="", to=to, intent="other", kind="twilio", note=f"error:{e}")
            return jsonify({"ok": False, "error": "twilio_send_error", "message": str(e)}), 502
    fake_sid = f"mock-{int(time.time()*1000)}"
    log_message("out", body, frm="", to=to, intent="other", kind="mock", note="mock-send", sid=fake_sid)
    return jsonify({"sid": fake_sid, "status": "mock-sent"})

@app.route("/inbound-sms", methods=["POST"])
def inbound_sms():
    form = request.form.to_dict()
    if validator:
        sig = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(request.url, form, sig):
            return jsonify({"ok": False, "error": "invalid_signature"}), 403
    frm  = form.get("From","").strip()
    to   = form.get("To","").strip()
    body = form.get("Body","").strip()
    sid  = form.get("MessageSid") or form.get("SmsSid")
    intent = detect_intent(body)
    log_message("in", body, frm=frm, to=to, intent=intent, kind="live" if TWILIO_READY else "mock",
                note="twilio-in", sid=sid)

    reply_text = None
    patient = find_patient_by_phone(frm)
    if patient:
        start_dt, dur, status = parse_natural_time(body)
        if start_dt:
            create_or_update_appt(patient["id"], patient.get("therapist") or "therapist",
                                  start_dt, dur or 60,
                                  status="confirmed" if intent=="confirm" else "pending",
                                  source="inbound", note="auto from inbound")
            reply_text = "Thanks! See you at the scheduled time."
        elif intent == "reschedule":
            reply_text = "Got it — reply with a preferred day/time (e.g., 'Friday 2pm')."
    if not reply_text:
        reply_text = "Thanks, we’ll follow up if needed."

    if MessagingResponse:
        twiml = MessagingResponse(); twiml.message(reply_text)
        return Response(str(twiml), mimetype="application/xml")
    return Response(reply_text, mimetype="text/plain")

@app.route("/status-callback", methods=["POST"])
def status_callback():
    return ("", 204)

# ─────────────────────────────────────────────────────────────────────────────
# Admin + Scheduling APIs (token required)
@app.route("/admin/messages", methods=["GET"])
def admin_messages():
    require_token()
    limit = max(1, min(500, int(request.args.get("limit", "50"))))
    q = (request.args.get("q") or "").lower().strip()
    with db() as con:
        rows = con.execute("SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    items = [dict(r) for r in rows]
    if q:
        items = [m for m in items if q in (m.get("body","").lower())]
    return jsonify({"messages": items})

@app.route("/admin/export.csv", methods=["GET"])
def admin_export_csv():
    require_token()
    with db() as con:
        rows = con.execute("SELECT * FROM messages ORDER BY ts DESC").fetchall()
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["ts","direction","kind","intent","from","to","body","note","sid"])
    for r in rows:
        w.writerow([r["ts"], r["direction"], r["kind"], r["intent"], r["frm"],
                    r["to_number"], r["body"], r["note"], r["sid"]])
    resp = make_response(out.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = 'attachment; filename="export.csv"'
    return resp

# Patients
@app.route("/patients", methods=["POST"])
def patients_create():
    require_token()
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    if not phone: return jsonify({"ok": False, "error": "phone_required"}), 400
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO patients
          (name,phone,address,city,state,zip,lat,lon,therapist,notes)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (data.get("name"), phone, data.get("address"), data.get("city"),
           data.get("state"), data.get("zip"), data.get("lat"), data.get("lon"),
           data.get("therapist"), data.get("notes")))
    return jsonify({"ok": True})

@app.route("/patients", methods=["GET"])
def patients_list():
    require_token()
    q = (request.args.get("q") or "").strip()
    with db() as con:
        if q:
            rows = con.execute("""SELECT * FROM patients
                                  WHERE name LIKE ? OR phone LIKE ? OR address LIKE ?
                                  ORDER BY name""", (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
        else:
            rows = con.execute("SELECT * FROM patients ORDER BY name").fetchall()
    return jsonify({"patients": [dict(r) for r in rows]})

# Schedule
@app.route("/schedule", methods=["POST"])
def schedule_create():
    require_token()
    data = request.get_json(silent=True) or {}
    pid = data.get("patient_id")
    if not pid: return jsonify({"ok": False, "error": "patient_id_required"}), 400
    start_str = data.get("start")
    dur = int(data.get("duration_min") or 60)
    therapist = data.get("therapist") or "therapist"
    note = data.get("note",""); status = data.get("status") or "pending"
    start_dt = None
    if start_str:
        try:
            start_dt = datetime.fromisoformat(start_str)
            if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=TZ)
        except Exception:
            start_dt, _, _ = parse_natural_time(start_str)
    if not start_dt:
        return jsonify({"ok": False, "error": "invalid_start"}), 400
    appt_id = create_or_update_appt(pid, therapist, start_dt, dur, status=status, source="manual", note=note)
    return jsonify({"ok": True, "appointment_id": appt_id, "start": start_dt.isoformat(), "duration_min": dur})

@app.route("/schedule", methods=["GET"])
def schedule_list():
    require_token()
    day = request.args.get("date")
    therapist = request.args.get("therapist")
    if not day: d = now_tz().date()
    else:      d = datetime.fromisoformat(day).date()
    appts = list_appointments(d, therapist)
    for a in appts:
        a["start_iso"] = from_epoch(a["start_ts"]).isoformat()
    return jsonify({"date": d.isoformat(), "therapist": therapist, "appointments": appts})

@app.route("/schedule/optimize", methods=["POST"])
def schedule_optimize():
    require_token()
    data = request.get_json(silent=True) or {}
    day = data.get("date"); therapist = data.get("therapist")
    if not day: return jsonify({"ok": False, "error": "date_required"}), 400
    d = datetime.fromisoformat(day).date()
    appts = list_appointments(d, therapist)
    ordered = optimize_route(appts)
    return jsonify({"ok": True, "date": d.isoformat(), "therapist": therapist, "appointments": ordered,
                    "drivetime": FEATURE_DRIVETIME_ROUTE})

# ─────────────────────────────────────────────────────────────────────────────
# Simple daily UI (token-protected)
UI_SCHEDULE = """
<!doctype html><meta charset="utf-8"><title>Schedule</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
body{font:15px system-ui;margin:16px}
h1{margin:0 0 10px}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,button,select{padding:8px;font:inherit}
.card{border:1px solid #ddd;border-radius:8px;padding:10px;margin:8px 0}
.row{display:flex;justify-content:space-between;gap:8px}
.small{color:#666;font-size:13px}
.badge{background:#eef;padding:2px 6px;border-radius:6px}
</style>
<h1>Day Schedule</h1>
<div class=controls>
  <label>Date <input id="d" type=date></label>
  <label>Therapist <input id="t" placeholder="(optional)"></label>
  <label>Token <input id="tok" placeholder="DEBUG_TOKEN" size=40></label>
  <button onclick="load()">Load</button>
  <button onclick="opt()">Optimize</button>
</div>
<div id="list"></div>
<script>
const fmt = ts => new Date(ts*1000).toLocaleString();
async function load(){
  const d=document.getElementById('d').value||new Date().toISOString().slice(0,10);
  const t=document.getElementById('t').value.trim();
  const tok=document.getElementById('tok').value.trim();
  const url = `/schedule?date=${d}` + (t?`&therapist=${encodeURIComponent(t)}`:'');
  const r=await fetch(url,{headers:{'X-Debug-Token':tok}});
  if(!r.ok){alert('Auth or load error');return}
  const j=await r.json(); const L=document.getElementById('list'); L.innerHTML='';
  if(!j.appointments.length){L.textContent='No appointments.'; return;}
  j.appointments.forEach(a=>{
    const div=document.createElement('div'); div.className='card';
    div.innerHTML = `
      <div class=row>
        <div><b>${a.patient_name||'(unknown)'}</b> <span class=badge>${a.status||''}</span></div>
        <div class=small>${a.start_iso}</div>
      </div>
      <div class=small>${a.address||''} ${a.city||''} ${a.state||''} ${a.zip||''}</div>
      <div class=small>${(a.lat!=null && a.lon!=null)?('Lat,Lon: '+a.lat+','+a.lon):''}</div>
      <div class=small>Therapist: ${a.therapist||''} • Duration: ${a.duration_min||60} min</div>
      <div class=small>${a.patient_phone?('<a target=_blank href="tel:'+a.patient_phone+'">Call</a>'):''}
        ${(a.lat!=null && a.lon!=null)?(' • <a target=_blank href="https://maps.google.com/?q='+a.lat+','+a.lon+'">Map</a>'):''}
      </div>`;
    L.appendChild(div);
  });
}
async function opt(){
  const d=document.getElementById('d').value||new Date().toISOString().slice(0,10);
  const t=document.getElementById('t').value.trim();
  const tok=document.getElementById('tok').value.trim();
  const r=await fetch('/schedule/optimize',{method:'POST',
     headers:{'X-Debug-Token':tok,'Content-Type':'application/json'},
     body:JSON.stringify({date:d,therapist:t||null})});
  if(!r.ok){alert('Auth or optimize error');return}
  const j=await r.json(); alert('Optimized. DriveTime='+(j.drivetime?'ON':'OFF'));
  load();
}
</script>
"""
@app.route("/ui/schedule", methods=["GET"])
def ui_schedule():
    if not FEATURE_UI_SCHEDULE: abort(404)
    require_token()
    return render_template_string(UI_SCHEDULE)

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)