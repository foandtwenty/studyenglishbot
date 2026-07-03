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

    def say(text):
        msg = _Msg()
        msg.text = text
        upd = SimpleNamespace(effective_chat=SimpleNamespace(id=100), message=msg)
        asyncio.run(bot.on_text(upd, ctx))

    return SimpleNamespace(ctx=ctx, press=press, say=say)


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


def test_type_mode_accepts_text_without_a_button(harness):
    """Type mode: the card itself is the prompt — typing is accepted right away,
    no «Написать» step."""
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:1")
    s = harness.ctx.user_data["session"]
    assert s["awaiting_input"] is True                # ready to accept text
    assert "type_answer" not in str(harness.ctx.bot.edits[-1].kb)   # no Написать button

    item = bot.current_item(s)
    harness.say(f"{item['v2']} {item['v3']}")         # correct answer typed directly
    assert "Верно" in harness.ctx.bot.edits[-1].text
    assert s["results"][bot.card_key(item)] is True


def test_type_mode_same_form_single_word(harness):
    """A V2==V3 verb (e.g. put/put/put) is accepted with one word."""
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:all")
    s = harness.ctx.user_data["session"]
    # find a same-form verb in the deck
    same = next(i for i in s["queue"]
                if set(bot._norm_forms(i["v2"])) == set(bot._norm_forms(i["v3"])))
    s["queue"].insert(s["pos"], same)                 # bring it to the front
    harness.say(same["v2"])                            # one word
    assert "Верно" in harness.ctx.bot.edits[-1].text


def test_reveal_shows_forms_and_marks_unknown(harness):
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:1")
    s = harness.ctx.user_data["session"]
    item = bot.current_item(s)
    harness.press("reveal")
    assert "Не помню" in harness.ctx.bot.edits[-1].text
    assert s["results"][bot.card_key(item)] is False


def test_stale_session_shows_notice(harness):
    harness.press("knew")                            # no active session
    assert "Активной тренировки нет" in harness.ctx.bot.edits[-1].text


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


def test_pause_keeps_session_and_offers_resume(harness):
    harness.press("pick:verbs")
    harness.press("lvl:1")
    s = harness.ctx.user_data["session"]
    bot.advance(s)                                     # simulate some progress
    harness.press("stop_session")                      # «Пауза»
    assert harness.ctx.user_data.get("session") is s   # session NOT discarded
    last = harness.ctx.bot.edits[-1]
    assert "паузе" in last.text
    assert "resume_session" in str(last.kb)            # «Продолжить» offered


def test_resume_continues_the_session(harness):
    harness.press("pick:verbs")
    harness.press("lvl:1")
    harness.press("stop_session")
    harness.press("resume_session")
    last = harness.ctx.bot.edits[-1].text
    assert "Пауза" in str(harness.ctx.bot.edits[-1].kb) or "/ " in last  # a card is shown again


def test_new_session_clears_paused(harness):
    harness.press("pick:verbs")
    harness.press("lvl:1")
    harness.press("stop_session")
    harness.press("new_session")
    assert harness.ctx.user_data.get("session") is None


def test_same_form_extra_wrong_word_is_wrong(harness):
    """Regression: «sat sitten» for sit/sat/sat must be wrong, not accepted on
    the first word alone."""
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:all")
    s = harness.ctx.user_data["session"]
    same = next(i for i in s["queue"]
                if set(bot._norm_forms(i["v2"])) == set(bot._norm_forms(i["v3"])))
    s["queue"].insert(s["pos"], same)
    harness.say(f"{same['v2']} totallywrong")
    assert "Верно" not in harness.ctx.bot.edits[-1].text


def test_hint_in_type_mode_marks_unknown(harness):
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:1")
    s = harness.ctx.user_data["session"]
    item = bot.current_item(s)
    harness.press("hint")
    assert bot.card_key(item) in s["ever_wrong"]          # hint = «ещё учу»
    harness.say(f"{item['v2']} {item['v3']}")             # correct, but hinted
    assert "подсказкой" in harness.ctx.bot.edits[-1].text
    # effective outcome counts it as not-known
    eff, known, unknown = bot._session_outcomes(s)
    assert eff[bot.card_key(item)] is False


def test_crafted_size_and_lvl_callbacks_do_not_crash(harness):
    """A bot-API client could send size:99 / lvl:xyz; these must be ignored,
    not raise KeyError/ValueError into the error handler."""
    harness.press("pick:verbs")
    harness.press("size:99")                          # not in size_map
    harness.press("lvl:xyz")                          # int() would raise
    assert harness.ctx.user_data.get("session") is None


def test_type_mode_applies_to_verb_card_in_mixed(harness):
    harness.press("toggle_mode")                      # type mode on
    harness.press("pick:mixed")
    harness.press("size:30")
    s = harness.ctx.user_data["session"]
    # advance to the first verb card and check it accepts input
    for _ in range(40):
        cur = bot.current_item(s)
        if cur is None:
            break
        if bot.item_type(cur) == "verbs":
            # re-render to apply gating for this card
            import asyncio
            asyncio.run(bot.show_card(100, s, harness.ctx.bot, type_mode=True))
            assert s["awaiting_input"] is True
            return
        ca = cur.get("answer") or cur.get("pattern") or cur.get("preposition")
        harness.press(f"ans:{ca}"); harness.press("next")
    # if no verb appeared in 30 mixed cards that's fine; nothing to assert


def test_topics_submenu_navigation(harness):
    harness.press("menu_topics")
    flat = str(harness.ctx.bot.edits[-1].kb)
    assert "pick:verbs" in flat and "pick:mixed" in flat and "back_to_types" in flat
    harness.press("pick:verbs")                       # → level selector
    assert "lvl:1" in str(harness.ctx.bot.edits[-1].kb)
    assert "menu_topics" in str(harness.ctx.bot.edits[-1].kb)   # back goes to topics


def test_main_menu_max_four_primary_buttons(harness):
    # with a paused session + due cards, still only 4 rows: продолжить / повторить
    # / выбрать тему / профиль
    harness.press("menu_topics"); harness.press("pick:verbs"); harness.press("lvl:1")
    harness.press("stop_session")                     # pause → main menu
    kb = harness.ctx.bot.edits[-1].kb
    assert len(kb.inline_keyboard) <= 4
    flat = str(kb)
    assert "resume_session" in flat and "menu_topics" in flat and "menu_profile" in flat


def test_undo_restores_state_after_wrong_answer(harness):
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:1")
    s = harness.ctx.user_data["session"]
    item = bot.current_item(s)
    key = bot.card_key(item)
    harness.say("totally wrong")                       # wrong -> marked + queued
    assert s["results"][key] is False and s["end_review"]
    harness.press("undo")                              # ↩️ Отмена on the result
    assert key not in s["results"]                     # mark reverted
    assert key not in s["ever_wrong"]
    assert s["review_buffer"] == [] and s["end_review"] == []
    assert s["pos"] == 0 and s["awaiting_input"] is True   # question re-shown
    harness.say(f"{item['v2']} {item['v3']}")          # answer again, correctly
    assert s["results"][key] is True


def test_undo_ignored_after_next(harness):
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:1")
    s = harness.ctx.user_data["session"]
    item = bot.current_item(s)
    harness.say(f"{item['v2']} {item['v3']}")
    harness.press("next")                              # advanced — undo now invalid
    pos = s["pos"]
    harness.press("undo")
    assert s["pos"] == pos                             # nothing happened
    assert s["results"][bot.card_key(item)] is True


def test_typo_answer_accepted_and_labelled(harness):
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:all")
    s = harness.ctx.user_data["session"]
    # find a verb with a long V3 eligible for typo forgiveness
    item = next(i for i in s["queue"]
                if len(i["v3"].split("/")[0]) >= 5 and i["v2"] != i["v3"])
    s["queue"].insert(s["pos"], item)
    v3 = item["v3"].split("/")[0]
    harness.say(f"{item['v2'].split('/')[0]} {v3[1]}{v3[0]}{v3[2:]}")  # swap first two letters
    assert "опечаткой" in harness.ctx.bot.edits[-1].text
    assert s["results"][bot.card_key(item)] is True    # counted as known


def test_interval_line_on_correct_answer(harness):
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:1")
    s = harness.ctx.user_data["session"]
    item = bot.current_item(s)
    harness.say(f"{item['v2']} {item['v3']}")
    assert "Повтор через 1 день" in harness.ctx.bot.edits[-1].text   # box 0 -> 1


def test_reverse_mode_full_flow(harness):
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("toggle_reverse")
    harness.press("lvl:1")
    s = harness.ctx.user_data["session"]
    item = bot.current_item(s)
    card = harness.ctx.bot.edits[-1].text
    assert item["translation"] in card                 # RU prompt shown
    assert item["v1"] not in card.split("💬")[0]        # the verb itself hidden
    harness.say(f"{item['v1']} {item['v2']} {item['v3']}")
    assert "Верно" in harness.ctx.bot.edits[-1].text
    assert s["results"][bot.card_key(item)] is True
    # forms-only answer (no V1) must be wrong in reverse mode
    harness.press("undo")
    harness.say(f"{item['v2']} {item['v3']}")
    assert s["results"][bot.card_key(item)] is False


def test_menu_done_state_after_daily(harness, monkeypatch):
    """After the daily plan is done, the menu celebrates and hooks tomorrow."""
    monkeypatch.setattr(bot, "_daily_counts", lambda uid: (0, 0))
    database.save_session(1, 5, 0, 5, {f"verbs::v{i}": True for i in range(5)})
    text, kb = bot.build_type_selector(user_id=1)
    assert "выполнена" in text
    assert "вернись завтра" in text
    assert "start_due" not in str(kb)            # no dead training button


def test_final_review_session_hides_repeat_and_hooks_tomorrow(harness):
    s = bot.new_session("review",
                        deck=[{"v1": "go", "v2": "went", "v3": "gone",
                               "translation": "т", "example": "e"}],
                        user_id=1)
    bot.mark_known(s, s["queue"][0])
    text, kb = bot.build_final(s, streak=2)
    assert "Тренировка дня выполнена" in text
    assert "repeat_session" not in str(kb)       # no SRS-contradicting rerun
    assert "В меню" in str(kb)


def test_wrong_answer_shows_return_note(harness):
    harness.press("pick:verbs")
    harness.press("toggle_mode")
    harness.press("lvl:1")
    harness.say("blatantly wrong")
    assert "Вернётся через пару карточек" in harness.ctx.bot.edits[-1].text


def test_weak_screen_offers_training(harness):
    database.save_session(1, 0, 2, 2, {"verbs::go": False, "prep::x": False})
    text, kb = bot.build_menu_weak(1)
    assert "train_weak" in str(kb)
    harness.press("train_weak")                  # cross-type error deck starts
    s = harness.ctx.user_data["session"]
    assert s is not None and s["exercise_type"] == "weak_all"
    assert len(s["queue"]) == 1                  # only verbs::go exists in content


def test_mixed_selector_has_verb_toggles(harness):
    text, kb = bot.build_size_selector("mixed", type_mode=False, user_id=1)
    flat = str(kb)
    assert "toggle_mode" in flat and "toggle_reverse" in flat


def test_progress_line_has_bar(harness):
    harness.press("pick:verbs")
    harness.press("lvl:1")
    assert "░" in harness.ctx.bot.edits[-1].text  # glanceable bar on the card


def test_history_renders_bars(harness):
    database.save_session(1, 8, 2, 10, {f"verbs::h{i}": i < 8 for i in range(10)})
    text, _ = bot.build_menu_history(1)
    assert "▓" in text and "8/10 (80%)" in text
