"""Integration coverage for the on_button dispatcher, driven through fake
Update/CallbackQuery objects. The fake query enforces Telegram's "answer a
callback exactly once" rule so double-answer regressions are caught."""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

import bot
import database


class _Msg:
    def __init__(self, mid=1, chat=100):
        self.message_id = mid
        self.chat_id = chat
        self.chat = SimpleNamespace(id=chat)
        self.text = ""

    async def delete(self):
        pass

    async def reply_text(self, *a, **k):
        pass


class _Query:
    def __init__(self, data, uid=1):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.message = _Msg()
        self.answers = []          # accepted answers (Telegram allows one)
        self._answered = False

    async def answer(self, text=None, show_alert=False):
        if self._answered:
            raise BadRequest("Query is too old or already answered")
        self._answered = True
        self.answers.append(text)


class _Bot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
        self.edits.append(SimpleNamespace(text=text, kb=reply_markup))

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        return _Msg(999)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "d.db"))
    database.init_db()
    database.ensure_user(1)
    ctx = SimpleNamespace(bot=_Bot(), user_data={})

    def press(data):
        q = _Query(data)
        asyncio.run(bot.on_button(SimpleNamespace(callback_query=q), ctx))
        return q

    return SimpleNamespace(ctx=ctx, press=press)


def test_normal_branch_acks_exactly_once(harness):
    q = harness.press("pick:verbs")
    assert q.answers == [None]                       # one empty ack, spinner cleared


def test_alert_branch_shows_alert_once_no_crash(harness):
    """Regression: the top-level ack + a second show_alert answer used to be a
    double-answer; the friendly alert must reach the user, not an error."""
    harness.press("pick:verbs")
    q = harness.press("size:weak")                   # no weak cards yet
    assert q.answers == ["Ошибок пока нет!"]         # exactly the alert, nothing else
    assert harness.ctx.user_data.get("session") is None


def test_pick_then_level_creates_session(harness):
    harness.press("pick:verbs")
    harness.press("lvl:1")                            # Базовый
    s = harness.ctx.user_data.get("session")
    assert s is not None and s["exercise_type"] == "verbs"
    assert len(s["queue"]) == len(bot._level_deck("verbs", 1))
    assert all(bot.item_level(i) == 1 for i in s["queue"])


def test_type_answer_reaches_input_prompt(harness):
    """Regression: «Написать» (type_answer) must open the input prompt, not be
    swallowed by the exercise-type selector."""
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:1")
    harness.press("type_answer")
    assert harness.ctx.user_data["session"]["awaiting_input"] is True
    assert "Напиши" in harness.ctx.bot.edits[-1].text


def test_stale_session_shows_notice(harness):
    harness.press("knew")                            # no active session
    assert "не найдена" in harness.ctx.bot.edits[-1].text


def test_main_menu_has_profile_not_service_buttons(harness):
    """Service buttons are collapsed under one Профиль entry on the main menu."""
    text, kb = bot.build_type_selector(user_id=1)
    flat = str(kb)
    assert "menu_profile" in flat
    assert "menu_stats" not in flat and "menu_history" not in flat


def test_profile_then_stats_then_back_navigation(harness):
    harness.press("menu_profile")
    assert "Профиль" in harness.ctx.bot.edits[-1].text
    flat = str(harness.ctx.bot.edits[-1].kb)
    assert "menu_stats" in flat and "menu_weak" in flat and "back_to_types" in flat
    harness.press("menu_stats")
    assert "Статистика" in harness.ctx.bot.edits[-1].text
    # back goes to profile, not main
    assert "menu_profile" in str(harness.ctx.bot.edits[-1].kb)
