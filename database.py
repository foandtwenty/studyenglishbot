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
                last_study TEXT,
                first_seen TEXT
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


def ensure_user(user_id: int) -> bool:
    """Insert user if not exists. Returns True if user is new."""
    today = date.today().isoformat()
    with _conn() as c:
        existing = c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            return False
        c.execute(
            "INSERT INTO users (user_id, streak, last_study, first_seen) VALUES (?,0,NULL,?)",
            (user_id, today),
        )
        return True


def save_session(user_id: int, known: int, unknown: int, total: int,
                 results: dict, exercise_type: str) -> int:
    """Save session results, update item stats and streak. Returns new streak value.

    Item stats are keyed as "<exercise_type>::<item_id>" so the same word in
    different exercises (e.g. verb "keep" vs pattern "keep") never collide.
    """
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (user_id, finished_at, known, unknown, total) VALUES (?,?,?,?,?)",
            (user_id, today, known, unknown, total),
        )
        for iid, is_known in results.items():
            key = f"{exercise_type}::{iid}"
            if is_known:
                c.execute("""
                    INSERT INTO verb_stats (user_id, verb_v1, known_count, unknown_count) VALUES (?,?,1,0)
                    ON CONFLICT(user_id, verb_v1) DO UPDATE SET known_count = known_count + 1
                """, (user_id, key))
            else:
                c.execute("""
                    INSERT INTO verb_stats (user_id, verb_v1, known_count, unknown_count) VALUES (?,?,0,1)
                    ON CONFLICT(user_id, verb_v1) DO UPDATE SET unknown_count = unknown_count + 1
                """, (user_id, key))

        row = c.execute("SELECT streak, last_study FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            c.execute("INSERT INTO users (user_id, streak, last_study, first_seen) VALUES (?,1,?,?)",
                      (user_id, today, today))
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


def get_weak_verbs(user_id: int, limit: int = 100) -> list:
    """Weak items across all exercises, ordered by error count.

    Keys are "<exercise_type>::<item_id>". The caller groups by type and may
    cap per category, so the default limit is high (effectively all).
    """
    with _conn() as c:
        return c.execute("""
            SELECT verb_v1, unknown_count, known_count
            FROM verb_stats
            WHERE user_id = ? AND unknown_count > 0
            ORDER BY unknown_count DESC, known_count ASC
            LIMIT ?
        """, (user_id, limit)).fetchall()


def get_weak_ids(user_id: int) -> dict:
    """Returns {"<exercise_type>::<item_id>": unknown_count} for items with errors."""
    with _conn() as c:
        rows = c.execute(
            "SELECT verb_v1, unknown_count FROM verb_stats WHERE user_id=? AND unknown_count > 0",
            (user_id,),
        ).fetchall()
    return {r["verb_v1"]: r["unknown_count"] for r in rows}


def get_lifetime_stats(user_id: int) -> dict:
    with _conn() as c:
        mastered = c.execute("""
            SELECT COUNT(*) FROM verb_stats
            WHERE user_id=? AND known_count > unknown_count
        """, (user_id,)).fetchone()[0]
        learning = c.execute("""
            SELECT COUNT(*) FROM verb_stats
            WHERE user_id=? AND unknown_count >= known_count AND unknown_count > 0
        """, (user_id,)).fetchone()[0]
        total_sessions = c.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        total_cards = c.execute(
            "SELECT COALESCE(SUM(total), 0) FROM sessions WHERE user_id=?", (user_id,)
        ).fetchone()[0]
    return {
        "mastered":     mastered,
        "learning":     learning,
        "sessions":     total_sessions,
        "total_cards":  total_cards,
    }


def get_history(user_id: int, limit: int = 10) -> list:
    with _conn() as c:
        return c.execute("""
            SELECT finished_at, known, unknown, total
            FROM sessions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()


def get_admin_stats() -> dict:
    today     = date.today().isoformat()
    week_ago  = (date.today() - timedelta(days=7)).isoformat()
    month_ago = (date.today() - timedelta(days=30)).isoformat()
    with _conn() as c:
        total_users    = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_7d      = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM sessions WHERE finished_at >= ?", (week_ago,)
        ).fetchone()[0]
        active_30d     = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM sessions WHERE finished_at >= ?", (month_ago,)
        ).fetchone()[0]
        total_sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        today_sessions = c.execute(
            "SELECT COUNT(*) FROM sessions WHERE finished_at = ?", (today,)
        ).fetchone()[0]
        daily = c.execute("""
            SELECT finished_at, COUNT(*) as cnt
            FROM sessions
            WHERE finished_at >= ?
            GROUP BY finished_at
            ORDER BY finished_at DESC
        """, (week_ago,)).fetchall()
    return {
        "total_users":    total_users,
        "active_7d":      active_7d,
        "active_30d":     active_30d,
        "total_sessions": total_sessions,
        "today_sessions": today_sessions,
        "daily":          daily,
    }
