import sqlite3
import json
import os
import secrets
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "workbook.db")


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            lesson_id TEXT NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(student_id, lesson_id),
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            lesson_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS teacher_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            lesson_id TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(student_id, lesson_id),
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- students ----------

def create_student(name: str) -> sqlite3.Row:
    token = secrets.token_urlsafe(6)
    conn = get_conn()
    conn.execute(
        "INSERT INTO students (token, name, created_at) VALUES (?, ?, ?)",
        (token, name, now_iso()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM students WHERE token = ?", (token,)).fetchone()
    conn.close()
    return row


def get_student_by_token(token: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM students WHERE token = ?", (token,)).fetchone()
    conn.close()
    return row


def get_student_by_id(student_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    conn.close()
    return row


def list_students():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM students ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def delete_student(student_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
    conn.commit()
    conn.close()


# ---------- progress ----------

def get_progress(student_id: int, lesson_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT state_json FROM progress WHERE student_id = ? AND lesson_id = ?",
        (student_id, lesson_id),
    ).fetchone()
    conn.close()
    if not row:
        return {}
    try:
        return json.loads(row["state_json"])
    except (TypeError, ValueError):
        return {}


def save_progress(student_id: int, lesson_id: str, patch: dict) -> dict:
    current = get_progress(student_id, lesson_id)
    current.update(patch)
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO progress (student_id, lesson_id, state_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(student_id, lesson_id)
        DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
        """,
        (student_id, lesson_id, json.dumps(current, ensure_ascii=False), now_iso()),
    )
    conn.commit()
    conn.close()
    return current


def all_progress_for_student(student_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT lesson_id, state_json, updated_at FROM progress WHERE student_id = ?",
        (student_id,),
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[r["lesson_id"]] = {
                "state": json.loads(r["state_json"]),
                "updated_at": r["updated_at"],
            }
        except (TypeError, ValueError):
            pass
    return result


# ---------- recordings ----------

def add_recording(student_id: int, lesson_id: str, filename: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO recordings (student_id, lesson_id, filename, uploaded_at) VALUES (?, ?, ?, ?)",
        (student_id, lesson_id, filename, now_iso()),
    )
    conn.commit()
    conn.close()


def list_recordings(student_id: int, lesson_id: str = None):
    conn = get_conn()
    if lesson_id:
        rows = conn.execute(
            "SELECT * FROM recordings WHERE student_id = ? AND lesson_id = ? ORDER BY uploaded_at DESC",
            (student_id, lesson_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM recordings WHERE student_id = ? ORDER BY uploaded_at DESC",
            (student_id,),
        ).fetchall()
    conn.close()
    return rows


# ---------- teacher feedback ----------

def get_feedback(student_id: int, lesson_id: str) -> str:
    conn = get_conn()
    row = conn.execute(
        "SELECT text FROM teacher_feedback WHERE student_id = ? AND lesson_id = ?",
        (student_id, lesson_id),
    ).fetchone()
    conn.close()
    return row["text"] if row else ""


def set_feedback(student_id: int, lesson_id: str, text: str):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO teacher_feedback (student_id, lesson_id, text, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(student_id, lesson_id)
        DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at
        """,
        (student_id, lesson_id, text, now_iso()),
    )
    conn.commit()
    conn.close()
