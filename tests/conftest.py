"""Shared fixtures and test doubles.

The bot uses long-polling and edits a single Telegram message, so the pure
logic (sessions, spaced repetition, stats) is fully testable without a network.
Async rendering is exercised with a FakeBot that records edit calls.
"""
import datetime as _dt
from types import SimpleNamespace

import pytest

import database
import bot


# ─── Database isolated per test ─────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh SQLite DB on a temp path, pointed to by both database and bot."""
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    return database


@pytest.fixture
def fake_date(monkeypatch):
    """Control database's notion of 'today' for streak tests.

    Returns a setter; database.date.today() yields the chosen real date so
    arithmetic with timedelta keeps working.
    """
    state = {"today": _dt.date(2026, 1, 10)}

    class _FakeDate:
        @classmethod
        def today(cls):
            return state["today"]

    monkeypatch.setattr(database, "date", _FakeDate)

    def _set(d: _dt.date):
        state["today"] = d

    return _set


# ─── Fake Telegram bot ──────────────────────────────────────────────────────

class FakeBot:
    """Records edit_message_text calls instead of hitting Telegram."""

    def __init__(self):
        self.edits = []

    async def edit_message_text(self, chat_id, message_id, text,
                                parse_mode=None, reply_markup=None):
        self.edits.append(SimpleNamespace(
            chat_id=chat_id, message_id=message_id, text=text,
            parse_mode=parse_mode, reply_markup=reply_markup,
        ))

    @property
    def last_text(self):
        return self.edits[-1].text if self.edits else None


@pytest.fixture
def fake_bot():
    return FakeBot()


# ─── Convenience builders ───────────────────────────────────────────────────

@pytest.fixture
def make_session():
    """Factory for a session over an explicit deck (no shuffling)."""
    def _make(exercise_type, deck, user_id=1, message_id=100):
        s = bot.new_session(exercise_type, deck=deck, user_id=user_id)
        s["message_id"] = message_id
        return s
    return _make
