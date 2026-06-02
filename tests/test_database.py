"""Database layer: user creation, namespaced item stats, streak across days,
lifetime aggregates, history."""
import datetime as _dt


def test_ensure_user_is_idempotent(db):
    assert db.ensure_user(1) is True
    assert db.ensure_user(1) is False


def test_save_session_namespaces_keys_no_collision(db):
    db.ensure_user(1)
    db.save_session(1, 1, 1, 2, {"keep": False, "go": True}, "verbs")
    db.save_session(1, 1, 1, 2, {"keep": False, "want": True}, "vp")
    ids = db.get_weak_ids(1)
    # "keep" errored in BOTH exercises and is tracked separately
    assert ids.get("verbs::keep") == 1
    assert ids.get("vp::keep") == 1
    # correct answers are not weak
    assert "verbs::go" not in ids and "vp::want" not in ids


def test_get_weak_verbs_ordered_by_error_count(db):
    db.ensure_user(1)
    db.save_session(1, 0, 1, 1, {"x": False}, "verbs")
    db.save_session(1, 0, 1, 1, {"x": False}, "verbs")   # x: 2 errors
    db.save_session(1, 0, 1, 1, {"y": False}, "verbs")   # y: 1 error
    rows = db.get_weak_verbs(1)
    counts = [r["unknown_count"] for r in rows]
    assert counts == sorted(counts, reverse=True)
    assert rows[0]["verb_v1"] == "verbs::x"


def test_lifetime_stats(db):
    db.ensure_user(1)
    for _ in range(5):                      # promote "go" to box 5 -> mastered
        db.save_session(1, 1, 0, 1, {"verbs::go": True}, None)
    db.save_session(1, 0, 1, 1, {"verbs::keep": False}, None)   # box 1 -> learning
    lt = db.get_lifetime_stats(1)
    assert lt["sessions"] == 6
    assert lt["total_cards"] == 6
    assert lt["mastered"] == 1     # box >= 5
    assert lt["learning"] == 1     # box 1–4


def test_history_newest_first(db):
    db.ensure_user(1)
    db.save_session(1, 1, 0, 1, {"a": True}, "verbs")
    db.save_session(1, 2, 0, 2, {"b": True, "c": True}, "verbs")
    rows = db.get_history(1)
    assert rows[0]["total"] == 2     # most recent first
    assert rows[1]["total"] == 1


def test_streak_increments_on_consecutive_days(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 10))
    assert db.save_session(1, 1, 0, 1, {"a": True}, "verbs") == 1
    fake_date(_dt.date(2026, 1, 11))
    assert db.save_session(1, 1, 0, 1, {"a": True}, "verbs") == 2
    fake_date(_dt.date(2026, 1, 12))
    assert db.save_session(1, 1, 0, 1, {"a": True}, "verbs") == 3


def test_streak_same_day_does_not_increment(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 10))
    db.save_session(1, 1, 0, 1, {"a": True}, "verbs")
    assert db.save_session(1, 1, 0, 1, {"b": True}, "verbs") == 1


def test_streak_resets_after_gap(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 10))
    db.save_session(1, 1, 0, 1, {"a": True}, "verbs")
    fake_date(_dt.date(2026, 1, 15))      # missed days
    assert db.save_session(1, 1, 0, 1, {"a": True}, "verbs") == 1


def test_get_streak_zero_when_stale(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 10))
    db.save_session(1, 1, 0, 1, {"a": True}, "verbs")
    fake_date(_dt.date(2026, 1, 20))      # long gap, no new session
    assert db.get_streak(1) == 0


def test_due_respects_user_timezone(db, monkeypatch):
    """A card due 'today' in the user's local frame is due regardless of where
    UTC midnight falls."""
    import datetime as _dt
    db.ensure_user(1)
    db.set_tz_offset(1, 5)                        # UTC+5
    db.save_session(1, 1, 0, 1, {"verbs::go": True}, None)   # box1 -> due in 1 day
    # Freeze UTC at a moment where local (UTC+5) date is already the due day.
    base = _dt.datetime.now(_dt.timezone.utc).replace(hour=20, minute=0)
    monkeypatch.setattr(db, "_now", lambda: base + _dt.timedelta(days=1))
    assert db.get_due_count(1) >= 1
