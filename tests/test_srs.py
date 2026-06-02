"""Spaced repetition (Leitner), interleaved decks, reminders."""
import datetime as _dt

import bot


# ─── Leitner scheduling (DB) ────────────────────────────────────────────────

def test_leitner_promotes_and_schedules(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 1))
    db.save_session(1, 1, 0, 1, {"go": True}, "verbs")     # box1 -> due +1
    assert db.get_due_count(1, "2026-01-01") == 0
    assert db.get_due_count(1, "2026-01-02") == 1
    fake_date(_dt.date(2026, 1, 2))
    db.save_session(1, 1, 0, 1, {"go": True}, "verbs")     # box2 -> due +2
    assert db.get_due_count(1, "2026-01-03") == 0
    assert db.get_due_count(1, "2026-01-04") == 1


def test_leitner_reset_on_error(db, fake_date):
    db.ensure_user(1)
    for d in (1, 2, 4):                                     # promote a few boxes
        fake_date(_dt.date(2026, 1, d))
        db.save_session(1, 1, 0, 1, {"go": True}, "verbs")
    fake_date(_dt.date(2026, 1, 8))
    db.save_session(1, 0, 1, 1, {"go": False}, "verbs")    # mistake -> box1, due +1
    assert db.get_due_count(1, "2026-01-08") == 0
    assert db.get_due_count(1, "2026-01-09") == 1


def test_due_ids_are_namespaced(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 1))
    db.save_session(1, 1, 0, 1, {"go": True}, "verbs")
    assert db.get_due_ids(1, "2026-01-02") == ["verbs::go"]


# ─── Reminders (DB) ─────────────────────────────────────────────────────────

def test_reminders_toggle(db):
    db.ensure_user(1)
    assert db.get_reminders(1) is True
    db.set_reminders(1, False)
    assert db.get_reminders(1) is False


def test_reminder_targets(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 1))
    db.save_session(1, 1, 0, 1, {"go": True}, "verbs")     # due 01-02, studied 01-01
    assert 1 in db.get_reminder_targets("2026-01-02")
    db.set_reminders(1, False)
    assert 1 not in db.get_reminder_targets("2026-01-02")  # opted out


def test_reminder_targets_excludes_studied_today(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 2))
    db.save_session(1, 1, 0, 1, {"go": True}, "verbs")     # studied today, due tomorrow
    assert 1 not in db.get_reminder_targets("2026-01-02")


# ─── Interleaving & review decks (bot) ──────────────────────────────────────

def test_item_type():
    assert bot.item_type({"v1": "go"}) == "verbs"
    assert bot.item_type({"verb": "want"}) == "vp"
    assert bot.item_type({"adjective": "afraid"}) == "adjprep"
    assert bot.item_type({"sentence": "x {?} y"}) == "prep"


def test_mixed_pool_is_all_cards():
    assert len(bot._mixed_pool()) == sum(len(v) for v in bot.CONTENT.values())


def test_new_session_mixed_interleaves():
    s = bot.new_session("mixed", size=24)
    types = {bot.item_type(x) for x in s["queue"]}
    assert len(types) >= 2                                  # genuinely mixed


def test_build_review_deck_returns_due_items(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 1))
    db.save_session(1, 1, 0, 1, {"go": True}, "verbs")
    fake_date(_dt.date(2026, 1, 2))
    deck = bot._build_review_deck(1)
    assert [bot.item_id(x) for x in deck] == ["go"]


# ─── Menus reflect the new features ─────────────────────────────────────────

def test_main_menu_shows_due_and_mixed(db, fake_date):
    db.ensure_user(1)
    fake_date(_dt.date(2026, 1, 1))
    db.save_session(1, 1, 0, 1, {"go": True}, "verbs")
    fake_date(_dt.date(2026, 1, 2))
    _, kb = bot.build_type_selector(user_id=1)
    flat = str(kb)
    assert "start_due" in flat
    assert "pick:mixed" in flat


def test_due_count_matches_deck_ignoring_orphans(db, monkeypatch):
    """The «К повторению» badge must equal what start_due actually opens, even
    when verb_stats holds keys for content that no longer exists."""
    import datetime as _dt
    db.ensure_user(1)
    db.save_session(1, 1, 0, 1, {"verbs::go": True}, None)     # real card
    db.save_session(1, 0, 1, 1, {"vp::__gone__": False}, None)  # orphaned key

    class FD:
        @classmethod
        def today(cls):
            return _dt.date.today() + _dt.timedelta(days=40)
    monkeypatch.setattr(db, "date", FD)

    assert db.get_due_count(1) == 2                 # raw rows include the orphan
    assert bot._due_count(1) == len(bot._build_review_deck(1)) == 1


def test_help_reminders_button_present_with_user(db):
    db.ensure_user(1)
    _, kb = bot.build_menu_help(1)
    assert "menu_reminders" in str(kb)
    _, kb2 = bot.build_menu_help()
    assert "menu_reminders" not in str(kb2)


def test_reminder_settings_screen_steppers(db):
    db.ensure_user(1)
    text, kb = bot.build_reminder_settings(1)
    flat = str(kb)
    assert "18:00" in text and "UTC" in text          # default time shown
    for cb in ("rem_hour_inc", "rem_hour_dec", "rem_tz_inc", "rem_tz_dec", "rem_toggle"):
        assert cb in flat


def test_reminder_settings_hour_wraps(db):
    db.ensure_user(1)
    db.set_reminder_hour(1, 23)
    db.set_reminder_hour(1, db.get_reminder_settings(1)["hour"] + 1)   # 24 -> 0
    assert db.get_reminder_settings(1)["hour"] == 0


def test_tz_offset_clamped(db):
    db.ensure_user(1)
    db.set_tz_offset(1, 99)
    assert db.get_reminder_settings(1)["tz"] == 14
    db.set_tz_offset(1, -99)
    assert db.get_reminder_settings(1)["tz"] == -12
