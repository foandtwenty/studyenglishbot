import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

DB_PATH = os.environ.get("DB_PATH", "study_english.db")


def _now() -> datetime:
    """Current UTC time. Indirected so tests can freeze it."""
    return datetime.now(timezone.utc)


def _user_today(c: sqlite3.Connection, user_id: int) -> date:
    """The user's local calendar date, derived from their tz_offset, so streaks
    and due dates respect their midnight rather than the server's."""
    row = c.execute("SELECT tz_offset FROM users WHERE user_id=?", (user_id,)).fetchone()
    tz = row["tz_offset"] if row and row["tz_offset"] is not None else 0
    return (_now() + timedelta(hours=tz)).date()

# Leitner spaced-repetition intervals (box -> days until next review).
# Correct answer promotes one box (longer interval); a mistake resets to box 1.
LEITNER_DAYS = {1: 1, 2: 2, 3: 4, 4: 7, 5: 15, 6: 30}
MAX_BOX = 6


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    # WAL lets readers and a writer coexist; busy_timeout waits out a brief lock
    # instead of failing immediately. Cheap insurance even for a single instance.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                streak        INTEGER DEFAULT 0,
                last_study    TEXT,
                first_seen    TEXT,
                reminders     INTEGER DEFAULT 1,
                reminder_hour INTEGER DEFAULT 18,
                tz_offset     INTEGER DEFAULT 0
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
                box           INTEGER DEFAULT 0,
                next_due      TEXT,
                first_seen    TEXT,
                PRIMARY KEY (user_id, verb_v1)
            );
            CREATE INDEX IF NOT EXISTS idx_verb_stats_due
                ON verb_stats (user_id, next_due);
            CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON sessions (user_id, id);
        """)
        # Migrations for databases created before these columns existed.
        vcols = {r["name"] for r in c.execute("PRAGMA table_info(verb_stats)")}
        if "box" not in vcols:
            c.execute("ALTER TABLE verb_stats ADD COLUMN box INTEGER DEFAULT 0")
        if "next_due" not in vcols:
            c.execute("ALTER TABLE verb_stats ADD COLUMN next_due TEXT")
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "reminders" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN reminders INTEGER DEFAULT 1")
        if "reminder_hour" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN reminder_hour INTEGER DEFAULT 18")
        if "tz_offset" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN tz_offset INTEGER DEFAULT 0")
        if "first_seen" not in vcols:
            c.execute("ALTER TABLE verb_stats ADD COLUMN first_seen TEXT")


def ensure_user(user_id: int) -> bool:
    """Insert user if not exists. Returns True if user is new."""
    today = _now().date().isoformat()        # UTC, consistent with the rest
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
                 results: dict, exercise_type: str | None = None) -> int:
    """Save session results, update item stats + Leitner schedule, and streak.
    Returns the new streak value.

    `results` keys are the namespaced item key "<exercise_type>::<item_id>".
    For convenience, pass bare ids plus `exercise_type` and they'll be
    namespaced here (used by tests / single-type callers).
    """
    with _conn() as c:
        today_d   = _user_today(c, user_id)
        today     = today_d.isoformat()
        yesterday = (today_d - timedelta(days=1)).isoformat()
        c.execute(
            "INSERT INTO sessions (user_id, finished_at, known, unknown, total) VALUES (?,?,?,?,?)",
            (user_id, today, known, unknown, total),
        )
        for raw_key, is_known in results.items():
            key = raw_key if exercise_type is None else f"{exercise_type}::{raw_key}"
            prev = c.execute(
                "SELECT box FROM verb_stats WHERE user_id=? AND verb_v1=?", (user_id, key)
            ).fetchone()
            box = (prev["box"] if prev and prev["box"] else 0)
            box = min(box + 1, MAX_BOX) if is_known else 1
            due = (today_d + timedelta(days=LEITNER_DAYS[box])).isoformat()
            kc, uc = (1, 0) if is_known else (0, 1)
            c.execute("""
                INSERT INTO verb_stats (user_id, verb_v1, known_count, unknown_count, box, next_due, first_seen)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(user_id, verb_v1) DO UPDATE SET
                    known_count   = known_count   + ?,
                    unknown_count = unknown_count + ?,
                    box           = ?,
                    next_due      = ?
            """, (user_id, key, kc, uc, box, due, today, kc, uc, box, due))

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
    with _conn() as c:
        yesterday = (_user_today(c, user_id) - timedelta(days=1)).isoformat()
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
    # "Mastered" = you get it right more often than wrong (known > unknown) —
    # intuitive and visible after the first good session. "Learning" = the rest
    # you've touched. (Spaced-repetition scheduling still uses the Leitner box.)
    with _conn() as c:
        mastered = c.execute(
            "SELECT COUNT(*) FROM verb_stats WHERE user_id=? AND known_count > unknown_count",
            (user_id,),
        ).fetchone()[0]
        learning = c.execute(
            "SELECT COUNT(*) FROM verb_stats WHERE user_id=? AND known_count <= unknown_count",
            (user_id,),
        ).fetchone()[0]
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


def get_new_today_count(user_id: int) -> int:
    """Cards that were first introduced today (their first_seen == today)."""
    with _conn() as c:
        today = _user_today(c, user_id).isoformat()
        return c.execute(
            "SELECT COUNT(*) FROM verb_stats WHERE user_id=? AND first_seen=?",
            (user_id, today),
        ).fetchone()[0]


def get_seen_keys(user_id: int) -> set:
    """Every namespaced key the user has ever answered (has a verb_stats row).
    Cards NOT in this set are brand-new and eligible for the daily new budget."""
    with _conn() as c:
        rows = c.execute("SELECT verb_v1 FROM verb_stats WHERE user_id=?", (user_id,)).fetchall()
    return {r["verb_v1"] for r in rows}


def get_known_keys(user_id: int) -> set:
    """Namespaced keys the user reliably knows (known_count > unknown_count),
    for per-level «выучено» progress."""
    with _conn() as c:
        rows = c.execute(
            "SELECT verb_v1 FROM verb_stats WHERE user_id=? AND known_count > unknown_count",
            (user_id,),
        ).fetchall()
    return {r["verb_v1"] for r in rows}


def get_due_ids(user_id: int, today: str | None = None) -> list:
    """Namespaced keys of cards whose spaced-repetition review is due."""
    with _conn() as c:
        today = today or _user_today(c, user_id).isoformat()
        rows = c.execute(
            "SELECT verb_v1 FROM verb_stats "
            "WHERE user_id=? AND next_due IS NOT NULL AND next_due <= ?",
            (user_id, today),
        ).fetchall()
    return [r["verb_v1"] for r in rows]


def get_due_count(user_id: int, today: str | None = None) -> int:
    with _conn() as c:
        today = today or _user_today(c, user_id).isoformat()
        return c.execute(
            "SELECT COUNT(*) FROM verb_stats "
            "WHERE user_id=? AND next_due IS NOT NULL AND next_due <= ?",
            (user_id, today),
        ).fetchone()[0]


def get_reminder_targets(utc_hour: int | None = None) -> list:
    """User ids to nudge: reminders on, a card due in THEIR local today, and no
    study yet in their local today. Every day boundary uses the user's
    tz_offset, consistent with how save_session stores last_study/next_due.

    If `utc_hour` is given, only users whose personal reminder hour maps to
    that UTC hour are included.
    """
    with _conn() as c:
        users = c.execute(
            "SELECT user_id, last_study, reminder_hour, tz_offset "
            "FROM users WHERE reminders = 1"
        ).fetchall()
        out = []
        for u in users:
            tz = u["tz_offset"] or 0
            if utc_hour is not None and (u["reminder_hour"] - tz) % 24 != utc_hour:
                continue
            today = (_now() + timedelta(hours=tz)).date().isoformat()
            if u["last_study"] and u["last_study"] >= today:
                continue                       # already studied in their today
            due = c.execute(
                "SELECT 1 FROM verb_stats WHERE user_id=? "
                "AND next_due IS NOT NULL AND next_due <= ? LIMIT 1",
                (u["user_id"], today),
            ).fetchone()
            if due:
                out.append(u["user_id"])
        return out


def set_reminders(user_id: int, enabled: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET reminders=? WHERE user_id=?", (1 if enabled else 0, user_id))


def get_reminders(user_id: int) -> bool:
    with _conn() as c:
        row = c.execute("SELECT reminders FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row["reminders"]) if row else True


def set_reminder_hour(user_id: int, hour: int) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET reminder_hour=? WHERE user_id=?", (hour % 24, user_id))


def set_tz_offset(user_id: int, offset: int) -> None:
    offset = max(-12, min(14, offset))
    with _conn() as c:
        c.execute("UPDATE users SET tz_offset=? WHERE user_id=?", (offset, user_id))


def get_reminder_settings(user_id: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT reminders, reminder_hour, tz_offset FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"enabled": True, "hour": 18, "tz": 0}
    return {"enabled": bool(row["reminders"]),
            "hour": row["reminder_hour"], "tz": row["tz_offset"]}


def get_history(user_id: int, limit: int = 10) -> list:
    with _conn() as c:
        return c.execute("""
            SELECT finished_at, known, unknown, total
            FROM sessions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()


def reset_all_progress() -> dict:
    """Wipe learning progress for ALL users — session history, per-card stats
    (boxes/due/errors), and streaks. Keeps user rows and reminder settings.
    Irreversible. Returns how many rows were cleared."""
    with _conn() as c:
        sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        stats    = c.execute("SELECT COUNT(*) FROM verb_stats").fetchone()[0]
        users    = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        c.execute("DELETE FROM sessions")
        c.execute("DELETE FROM verb_stats")
        c.execute("UPDATE users SET streak = 0, last_study = NULL")
    return {"sessions": sessions, "verb_stats": stats, "users": users}


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
