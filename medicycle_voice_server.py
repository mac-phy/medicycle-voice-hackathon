"""
Vocare voice adherence server.

All schedules, records, follow-ups and call timing live here, in application
code and the database. The voice agent only talks and calls these endpoints;
it never decides what is due or what to follow up on.

Agent tool endpoints:
  POST /get-due-medications   what is scheduled today
  POST /log-dose              { "medication", "taken", "reason_missed" }
  POST /check-missed-streak   { "medication" }
  POST /report-concern        { "concern", "medication" }
  POST /get-patient-history   recent check-ins + open follow-ups (computed here)
  POST /resolve-follow-up     { "id", "outcome" }
  POST /stop-calls            patient asked not to be called

Operations:
  GET  /run-scheduler?token=...   called every few minutes by an external pinger
  GET  /call-queue                calls that are due / placed
  GET  /escalations               what a care team reviews

Run locally with:
  python medicycle_voice_server.py

Then expose it publicly with ngrok:
  ngrok http 5001

Copy the https://xxxx.ngrok-free.app URL into agents/medicycle-voice.jsonc's
"tools" -> "http" -> "url" fields (both log_dose_status and flag_missed_doses).

Note: this is a standalone hackathon service that mirrors MediCycle's real
adherence-logging concept without touching the production backend. Same
underlying idea (dose logged, missed-dose streaks tracked, escalation
triggered), safe to build and demo fast.
"""

import os
import sqlite3
import base64
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)
DB_PATH = "adherence.db"

# Escalate after this many consecutive missed doses for the same medication.
MISSED_DOSE_ESCALATION_THRESHOLD = 2

# For the demo, all calls are treated as one patient. In a real integration
# this would come from caller ID / patient lookup against MediCycle's auth.
DEMO_PATIENT = "demo_patient"

# Nairobi is UTC+3 all year, no daylight saving, so a fixed offset is exact.
EAT = timezone(timedelta(hours=3))
SCHEDULER_TOKEN = os.environ.get("SCHEDULER_TOKEN", "")
# A call is placed only if we are within this many minutes after its time.
CALL_WINDOW_MINUTES = 90
# How far back follow-ups look.
HISTORY_DAYS = 7


def now_eat():
    return datetime.now(EAT)


def today_str():
    return now_eat().strftime("%Y-%m-%d")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dose_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient TEXT NOT NULL,
            medication TEXT NOT NULL,
            taken INTEGER NOT NULL,
            reason_missed TEXT,
            logged_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS medication_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient TEXT NOT NULL,
            medication TEXT NOT NULL,
            dosage TEXT,
            schedule_time TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient TEXT NOT NULL,
            kind TEXT NOT NULL,            -- 'missed_doses' or 'patient_concern'
            medication TEXT,
            detail TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient TEXT NOT NULL,
            kind TEXT NOT NULL,        -- refill | side_effect | concern | unconfirmed_dose
            medication TEXT,
            question TEXT NOT NULL,    -- what the agent should ask
            ref_date TEXT NOT NULL,    -- the day this arose; dedupes repeats
            status TEXT NOT NULL DEFAULT 'open',
            outcome TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (patient, kind, medication, ref_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient TEXT NOT NULL,
            kind TEXT NOT NULL,        -- reminder | checkin
            call_time TEXT NOT NULL,   -- HH:MM Nairobi time
            phone TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient TEXT NOT NULL,
            kind TEXT NOT NULL,
            call_date TEXT NOT NULL,
            status TEXT NOT NULL,      -- queued | placed | failed
            detail TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (patient, kind, call_date)   -- one call of each kind per day
        )
    """)
    conn.commit()

    # Seed a demo patient's medication schedule, only if empty.
    existing = conn.execute("SELECT COUNT(*) AS c FROM medication_schedule").fetchone()["c"]
    if existing == 0:
        conn.executemany(
            "INSERT INTO medication_schedule (patient, medication, dosage, schedule_time) VALUES (?, ?, ?, ?)",
            [
                (DEMO_PATIENT, "Lisinopril", "10mg", "morning"),
                (DEMO_PATIENT, "Metformin", "500mg", "morning"),
            ],
        )
        conn.commit()
    if conn.execute("SELECT COUNT(*) AS c FROM call_schedule").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO call_schedule (patient, kind, call_time, phone) VALUES (?, ?, ?, ?)",
            [
                (DEMO_PATIENT, "reminder", "08:00", os.environ.get("PATIENT_PHONE")),
                (DEMO_PATIENT, "checkin", "20:00", os.environ.get("PATIENT_PHONE")),
            ],
        )
        conn.commit()
    conn.close()


@app.route("/get-due-medications", methods=["POST"])
def get_due_medications():
    conn = get_db()
    rows = conn.execute(
        "SELECT medication, dosage, schedule_time FROM medication_schedule WHERE patient = ?",
        (DEMO_PATIENT,),
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({"medications": [], "message": "No medications scheduled for this patient."})

    meds = [
        {"medication": r["medication"], "dosage": r["dosage"], "schedule_time": r["schedule_time"]}
        for r in rows
    ]
    return jsonify({"medications": meds})


@app.route("/log-dose", methods=["POST"])
def log_dose():
    data = request.get_json(force=True) or {}
    medication = (data.get("medication") or "").strip()
    taken = bool(data.get("taken"))
    reason_missed = (data.get("reason_missed") or "").strip()

    if not medication:
        return jsonify({"success": False, "message": "No medication specified."}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO dose_log (patient, medication, taken, reason_missed, logged_at) VALUES (?, ?, ?, ?, ?)",
        (DEMO_PATIENT, medication, int(taken), reason_missed, now_eat().isoformat()),
    )
    conn.commit()
    conn.close()

    status = "taken" if taken else f"missed ({reason_missed or 'no reason given'})"
    return jsonify({
        "success": True,
        "message": f"Logged {medication} as {status}."
    })


@app.route("/check-missed-streak", methods=["POST"])
def check_missed_streak():
    data = request.get_json(force=True) or {}
    medication = (data.get("medication") or "").strip()

    if not medication:
        return jsonify({"escalate": False, "message": "No medication specified."}), 400

    conn = get_db()
    # Most recent entries first for this patient + medication.
    rows = conn.execute(
        "SELECT taken FROM dose_log WHERE patient = ? AND medication = ? ORDER BY logged_at DESC, id DESC",
        (DEMO_PATIENT, medication),
    ).fetchall()

    streak = 0
    for row in rows:
        if row["taken"] == 0:
            streak += 1
        else:
            break

    escalate = streak >= MISSED_DOSE_ESCALATION_THRESHOLD

    # Make the escalation real: record it for the care team, once per open streak,
    # instead of relying on the agent merely saying someone will follow up.
    if escalate:
        already_open = conn.execute(
            "SELECT 1 FROM escalations WHERE patient = ? AND kind = 'missed_doses' "
            "AND medication = ? AND status = 'open'",
            (DEMO_PATIENT, medication),
        ).fetchone()
        if not already_open:
            conn.execute(
                "INSERT INTO escalations (patient, kind, medication, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (DEMO_PATIENT, "missed_doses", medication,
                 f"{streak} consecutive missed doses", now_eat().isoformat()),
            )
            conn.commit()
    conn.close()

    return jsonify({
        "escalate": escalate,
        "missed_streak": streak,
        "message": (
            f"Patient has missed {medication} {streak} time(s) in a row. "
            + ("Escalating to care team." if escalate else "No escalation needed yet.")
        )
    })


@app.route("/report-concern", methods=["POST"])
def report_concern():
    """Record something the patient said that a human should see (e.g. dizziness).
    The agent passes the patient's own words; it never interprets or diagnoses."""
    data = request.get_json(force=True) or {}
    concern = (data.get("concern") or "").strip()
    medication = (data.get("medication") or "").strip()

    if not concern:
        return jsonify({"success": False, "message": "No concern provided."}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO escalations (patient, kind, medication, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (DEMO_PATIENT, "patient_concern", medication or None, concern, now_eat().isoformat()),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Concern recorded for the care team."})


@app.route("/escalations", methods=["GET"])
def list_escalations():
    """What a care team would review. Open items first, newest first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, kind, medication, detail, status, created_at FROM escalations "
        "WHERE patient = ? ORDER BY (status = 'open') DESC, created_at DESC",
        (DEMO_PATIENT,),
    ).fetchall()
    conn.close()
    return jsonify({"escalations": [dict(r) for r in rows]})


def compute_follow_ups(conn):
    """Work out what is worth asking about, from records. Plain rules, no model.
    Each follow-up is unique per (kind, medication, day) so it is created once."""
    since = (now_eat() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    today = today_str()
    created = now_eat().isoformat()

    def add(kind, med, question, ref):
        conn.execute(
            "INSERT OR IGNORE INTO follow_ups (patient, kind, medication, question, ref_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (DEMO_PATIENT, kind, med, question, ref, created),
        )

    # Missed doses with a recorded reason.
    for r in conn.execute(
        "SELECT medication, reason_missed, substr(logged_at,1,10) AS d FROM dose_log "
        "WHERE patient = ? AND taken = 0 AND substr(logged_at,1,10) >= ? AND substr(logged_at,1,10) < ?",
        (DEMO_PATIENT, since, today),
    ).fetchall():
        reason = (r["reason_missed"] or "").lower().replace(" ", "_")
        if reason == "ran_out":
            add("refill", r["medication"],
                f"Last time you said you had run out of {r['medication']}. Have you been able to get more?", r["d"])
        elif reason == "side_effects":
            add("side_effect", r["medication"],
                f"Last time you mentioned side effects from {r['medication']}. How are you finding it now?", r["d"])

    # Concerns the patient raised that are still open.
    for r in conn.execute(
        "SELECT medication, detail, substr(created_at,1,10) AS d FROM escalations "
        "WHERE patient = ? AND kind = 'patient_concern' AND status = 'open' "
        "AND substr(created_at,1,10) >= ? AND substr(created_at,1,10) < ?",
        (DEMO_PATIENT, since, today),
    ).fetchall():
        add("concern", r["medication"],
            f"Earlier you told us: \"{r['detail']}\". Is that any better today?", r["d"])

    # Scheduled medications with no entry at all on a past day, from the first day we have data.
    first = conn.execute("SELECT MIN(substr(logged_at,1,10)) AS d FROM dose_log WHERE patient = ?",
                         (DEMO_PATIENT,)).fetchone()["d"]
    if first:
        meds = [m["medication"] for m in conn.execute(
            "SELECT medication FROM medication_schedule WHERE patient = ?", (DEMO_PATIENT,)).fetchall()]
        day = datetime.strptime(max(first, since), "%Y-%m-%d")
        end = datetime.strptime(today, "%Y-%m-%d")
        while day < end:
            ds = day.strftime("%Y-%m-%d")
            for med in meds:
                seen = conn.execute(
                    "SELECT 1 FROM dose_log WHERE patient = ? AND medication = ? AND substr(logged_at,1,10) = ?",
                    (DEMO_PATIENT, med, ds)).fetchone()
                if not seen:
                    add("unconfirmed_dose", med,
                        f"We never confirmed your {med} on {ds}. Did you take it that day?", ds)
            day += timedelta(days=1)
    conn.commit()


@app.route("/get-patient-history", methods=["POST"])
def get_patient_history():
    conn = get_db()
    compute_follow_ups(conn)
    since = (now_eat() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    days = {}
    for r in conn.execute(
        "SELECT medication, taken, reason_missed, substr(logged_at,1,10) AS d FROM dose_log "
        "WHERE patient = ? AND substr(logged_at,1,10) >= ? ORDER BY logged_at",
        (DEMO_PATIENT, since)).fetchall():
        entry = "taken" if r["taken"] else f"missed ({r['reason_missed'] or 'no reason'})"
        days.setdefault(r["d"], []).append(f"{r['medication']}: {entry}")
    follow_ups = [dict(r) for r in conn.execute(
        "SELECT id, kind, medication, question FROM follow_ups "
        "WHERE patient = ? AND status = 'open' ORDER BY created_at, id", (DEMO_PATIENT,)).fetchall()]
    conn.close()
    return jsonify({
        "recent_days": [{"date": d, "doses": v} for d, v in sorted(days.items())],
        "open_follow_ups": follow_ups,
        "instruction": "Ask each open follow-up question once, briefly, then call resolve_follow_up with the patient's answer.",
    })


@app.route("/resolve-follow-up", methods=["POST"])
def resolve_follow_up():
    data = request.get_json(force=True) or {}
    try:
        fid = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "A numeric follow-up id is required."}), 400
    outcome = (data.get("outcome") or "").strip()
    conn = get_db()
    cur = conn.execute(
        "UPDATE follow_ups SET status = 'resolved', outcome = ? WHERE id = ? AND patient = ? AND status = 'open'",
        (outcome, fid, DEMO_PATIENT))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"success": False, "message": "No open follow-up with that id."})
    return jsonify({"success": True, "message": "Follow-up closed."})


@app.route("/stop-calls", methods=["POST"])
def stop_calls():
    """The patient asked not to be called. Honoured immediately by the scheduler."""
    conn = get_db()
    conn.execute("UPDATE call_schedule SET active = 0 WHERE patient = ?", (DEMO_PATIENT,))
    conn.execute(
        "INSERT INTO escalations (patient, kind, detail, created_at) VALUES (?, 'patient_concern', ?, ?)",
        (DEMO_PATIENT, "Patient asked to stop automated calls.", now_eat().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Understood. Calls are stopped and the care team is told."})


def place_call(phone, kind):
    """Start an outbound call through Twilio. Returns (status, detail).
    Needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER and
    BRIDGE_URL (public https URL of the media bridge that serves /outbound-twiml)."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_PHONE_NUMBER")
    bridge = os.environ.get("BRIDGE_URL", "").rstrip("/")
    if not (sid and tok and frm and bridge and phone):
        return "queued", "Twilio or bridge not configured yet; recorded as due."
    body = urllib.parse.urlencode(
        {"To": phone, "From": frm, "Url": f"{bridge}/outbound-twiml?kind={kind}"}).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return "placed", f"Twilio accepted the call (HTTP {resp.status})."
    except Exception as e:  # network, auth, unverified trial number...
        return "failed", str(e)[:200]


@app.route("/run-scheduler", methods=["GET", "POST"])
def run_scheduler():
    """Hit every few minutes by an external pinger (Render free has no cron).
    Each (patient, kind, day) is claimed once, so repeated pings never double-call."""
    if not SCHEDULER_TOKEN or request.args.get("token") != SCHEDULER_TOKEN:
        return jsonify({"error": "forbidden"}), 403
    now = now_eat()
    today = today_str()
    results = []
    conn = get_db()
    for s in conn.execute("SELECT * FROM call_schedule WHERE active = 1").fetchall():
        hh, mm = map(int, s["call_time"].split(":"))
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if not (due <= now <= due + timedelta(minutes=CALL_WINDOW_MINUTES)):
            continue
        try:
            conn.execute(
                "INSERT INTO call_log (patient, kind, call_date, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
                (s["patient"], s["kind"], today, now.isoformat()))
            conn.commit()
        except sqlite3.IntegrityError:
            continue  # already handled today
        status, detail = place_call(s["phone"], s["kind"])
        conn.execute("UPDATE call_log SET status = ?, detail = ? WHERE patient = ? AND kind = ? AND call_date = ?",
                     (status, detail, s["patient"], s["kind"], today))
        conn.commit()
        results.append({"kind": s["kind"], "status": status, "detail": detail})
    conn.close()
    return jsonify({"time": now.isoformat(), "calls": results})


@app.route("/call-queue", methods=["GET"])
def call_queue():
    conn = get_db()
    rows = conn.execute("SELECT kind, call_date, status, detail FROM call_log "
                        "WHERE patient = ? ORDER BY id DESC LIMIT 30", (DEMO_PATIENT,)).fetchall()
    conn.close()
    return jsonify({"calls": [dict(r) for r in rows]})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Vocare voice adherence server is running"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5001, debug=os.environ.get("FLASK_DEBUG") == "1")