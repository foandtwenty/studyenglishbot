import os
import sqlite3
from datetime import date, timedelta

DB_PATH = os.environ.get("DB_PATH", "study_english.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                streak     INTEGER DEFAULT 0,
                last_study TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                finished_at TEXT NOT NULL,
                known       INTEGER NOT NULL,
                unknown     INTEGER NOT NULL,
                total       INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verb_stats (
                user_id       INTEGER NOT NULL,
                verb_v1       TEXT NOT NULL,
                known_count   INTEGER DEFAULT 0,
                unknown_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, verb_v1)
            );
        """)


def save_session(user_id: int, known: int, unknown: int, total: int, results: dict) -> int:
    """Save session results, update verb stats and streak. Returns new streak value."""
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (user_id, finished_at, known, unknown, total) VALUES (?,?,?,?,?)",
            (user_id, today, known, unknown, total),
        )

        for v1, is_known in results.items():
            if is_known:
                c.execute("""
                    INSERT INTO verb_stats (user_id, verb_v1, known_count, unknown_count) VALUES (?,?,1,0)
                    ON CONFLICT(user_id, verb_v1) DO UPDATE SET known_count = known_count + 1
                """, (user_id, v1))
            else:
                c.execute("""
                    INSERT INTO verb_stats (user_id, verb_v1, known_count, unknown_count) VALUES (?,?,0,1)
                    ON CONFLICT(user_id, verb_v1) DO UPDATE SET unknown_count = unknown_count + 1
                """, (user_id, v1))

        row = c.execute("SELECT streak, last_study FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            c.execute("INSERT INTO users (user_id, streak, last_study) VALUES (?,1,?)", (user_id, today))
            return 1

        streak, last = row["streak"], row["last_study"]
        if last == today:
            return streak
        new_streak = streak + 1 if last == yesterday else 1
        c.execute("UPDATE users SET streak=?, last_study=? WHERE user_id=?", (new_streak, today, user_id))
        return new_streak


def get_streak(user_id: int) -> int:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with _conn() as c:
        row = c.execute("SELECT streak, last_study FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return 0
        if row["last_study"] and row["last_study"] < yesterday:
            return 0
        return row["streak"]


def get_weak_verbs(user_id: int, limit: int = 10) -> list:
    with _conn() as c:
        return c.execute("""
            SELECT verb_v1, unknown_count, known_count
            FROM verb_stats
            WHERE user_id = ? AND unknown_count > 0
            ORDER BY unknown_count DESC, known_count ASC
            LIMIT ?
        """, (user_id, limit)).fetchall()


def get_history(user_id: int, limit: int = 5) -> list:
    with _conn() as c:
        return c.execute("""
            SELECT finished_at, known, unknown, total
            FROM sessions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
