"""
MediCycle Voice adherence server.

Two endpoints for the agent's HTTP tools:

  POST /log-dose              { "medication": "Metformin", "taken": true, "reason_missed": "" }
  POST /check-missed-streak   { "medication": "Metformin" }

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

import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)
DB_PATH = "adherence.db"

# Escalate after this many consecutive missed doses for the same medication.
MISSED_DOSE_ESCALATION_THRESHOLD = 2

# For the demo, all calls are treated as one patient. In a real integration
# this would come from caller ID / patient lookup against MediCycle's auth.
DEMO_PATIENT = "demo_patient"


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
        (DEMO_PATIENT, medication, int(taken), reason_missed, datetime.now().isoformat()),
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
                 f"{streak} consecutive missed doses", datetime.now().isoformat()),
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
        (DEMO_PATIENT, "patient_concern", medication or None, concern, datetime.now().isoformat()),
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


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Vocare voice adherence server is running"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5001, debug=True)