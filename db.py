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

        CREATE TABLE IF NOT EXISTS lesson_script (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            script_text TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(lesson_id, stage)
        );

        CREATE TABLE IF NOT EXISTS lesson_audio_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            original_filename TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(lesson_id, stage)
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


# ---------- 백업 / 복원 (Render 무료 요금제의 재배포시 데이터 초기화 대비) ----------

def export_all():
    conn = get_conn()
    students = [dict(r) for r in conn.execute("SELECT * FROM students").fetchall()]
    progress = [
        dict(r) for r in conn.execute(
            """
            SELECT s.token as token, p.lesson_id as lesson_id, p.state_json as state_json, p.updated_at as updated_at
            FROM progress p JOIN students s ON s.id = p.student_id
            """
        ).fetchall()
    ]
    feedback = [
        dict(r) for r in conn.execute(
            """
            SELECT s.token as token, f.lesson_id as lesson_id, f.text as text, f.updated_at as updated_at
            FROM teacher_feedback f JOIN students s ON s.id = f.student_id
            """
        ).fetchall()
    ]
    recordings = [
        dict(r) for r in conn.execute(
            """
            SELECT s.token as token, r.lesson_id as lesson_id, r.filename as filename, r.uploaded_at as uploaded_at
            FROM recordings r JOIN students s ON s.id = r.student_id
            """
        ).fetchall()
    ]
    lesson_scripts = [dict(r) for r in conn.execute("SELECT lesson_id, stage, script_text, updated_at FROM lesson_script").fetchall()]
    lesson_audio_meta = [dict(r) for r in conn.execute("SELECT lesson_id, stage, original_filename, updated_at FROM lesson_audio_meta").fetchall()]
    conn.close()
    return {
        "students": students,
        "progress": progress,
        "feedback": feedback,
        "recordings": recordings,
        "lesson_scripts": lesson_scripts,
        "lesson_audio_meta": lesson_audio_meta,
    }


def restore_student(token: str, name: str, created_at: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO students (token, name, created_at) VALUES (?, ?, ?)",
        (token, name, created_at),
    )
    conn.commit()
    conn.close()


def _student_id_by_token(conn, token: str):
    row = conn.execute("SELECT id FROM students WHERE token = ?", (token,)).fetchone()
    return row["id"] if row else None


def restore_progress_row(token: str, lesson_id: str, state_json: str, updated_at: str):
    conn = get_conn()
    sid = _student_id_by_token(conn, token)
    if sid:
        conn.execute(
            """
            INSERT INTO progress (student_id, lesson_id, state_json, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, lesson_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
            """,
            (sid, lesson_id, state_json, updated_at),
        )
        conn.commit()
    conn.close()


def restore_feedback_row(token: str, lesson_id: str, text: str, updated_at: str):
    conn = get_conn()
    sid = _student_id_by_token(conn, token)
    if sid:
        conn.execute(
            """
            INSERT INTO teacher_feedback (student_id, lesson_id, text, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, lesson_id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at
            """,
            (sid, lesson_id, text, updated_at),
        )
        conn.commit()
    conn.close()


def restore_recording_row(token: str, lesson_id: str, filename: str, uploaded_at: str):
    conn = get_conn()
    sid = _student_id_by_token(conn, token)
    if sid:
        exists = conn.execute(
            "SELECT 1 FROM recordings WHERE student_id = ? AND filename = ?", (sid, filename)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO recordings (student_id, lesson_id, filename, uploaded_at) VALUES (?, ?, ?, ?)",
                (sid, lesson_id, filename, uploaded_at),
            )
            conn.commit()
    conn.close()


# ---------- lesson audio metadata (선생님이 올린 원래 파일명 기억) ----------

def get_audio_original_name(lesson_id: str, stage: str) -> str:
    conn = get_conn()
    row = conn.execute(
        "SELECT original_filename FROM lesson_audio_meta WHERE lesson_id = ? AND stage = ?",
        (lesson_id, stage),
    ).fetchone()
    conn.close()
    return row["original_filename"] if row else ""


def set_audio_original_name(lesson_id: str, stage: str, filename: str):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO lesson_audio_meta (lesson_id, stage, original_filename, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(lesson_id, stage)
        DO UPDATE SET original_filename = excluded.original_filename, updated_at = excluded.updated_at
        """,
        (lesson_id, stage, filename, now_iso()),
    )
    conn.commit()
    conn.close()


# ---------- lesson scripts (예습/복습 스크립트, 관리자 페이지에서 편집) ----------

def get_script(lesson_id: str, stage: str) -> str:
    conn = get_conn()
    row = conn.execute(
        "SELECT script_text FROM lesson_script WHERE lesson_id = ? AND stage = ?",
        (lesson_id, stage),
    ).fetchone()
    conn.close()
    return row["script_text"] if row else ""


def set_script(lesson_id: str, stage: str, text: str):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO lesson_script (lesson_id, stage, script_text, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(lesson_id, stage)
        DO UPDATE SET script_text = excluded.script_text, updated_at = excluded.updated_at
        """,
        (lesson_id, stage, text, now_iso()),
    )
    conn.commit()
    conn.close()
