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
        "SELECT taken FROM dose_log WHERE patient = ? AND medication = ? ORDER BY logged_at DESC",
        (DEMO_PATIENT, medication),
    ).fetchall()
    conn.close()

    streak = 0
    for row in rows:
        if row["taken"] == 0:
            streak += 1
        else:
            break

    escalate = streak >= MISSED_DOSE_ESCALATION_THRESHOLD
    return jsonify({
        "escalate": escalate,
        "missed_streak": streak,
        "message": (
            f"Patient has missed {medication} {streak} time(s) in a row. "
            + ("Escalating to care team." if escalate else "No escalation needed yet.")
        )
    })


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "MediCycle Voice adherence server is running"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5001, debug=True)