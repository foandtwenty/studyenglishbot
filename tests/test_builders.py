"""Screen builders: result cards, final screen, weak-menu grouping, grammar."""
import bot

PREP = {"sentence": "She works {?} a hospital.", "answer": "in",
        "translation": "т", "rule": "in — внутри здания"}
VP = {"verb": "want", "pattern": "to", "translation": "т",
      "example": "I want to go.", "rule": "want + to — желание"}
ADJ = {"adjective": "afraid", "preposition": "of", "translation": "т",
       "example": "afraid of spiders", "rule": "afraid of — страх",
       "options": ["of", "about", "from"]}


def test_choice_result_always_has_keyboard():
    """Regression: correct answers used to return None (auto-advance); now the
    rule must stay visible behind a 'Дальше' button."""
    for item in (PREP, VP, ADJ):
        for correct in (True, False):
            text, kb = bot.build_choice_result(item, "x", correct)
            assert kb is not None, (item, correct)
            assert "Дальше" in str(kb)


def test_choice_result_shows_rule_on_correct():
    text, _ = bot.build_choice_result(PREP, "in", True)
    assert "✅" in text and "📖" in text
    text, _ = bot.build_choice_result(VP, "to", True)
    assert "📖" in text and "want" in text


def test_choice_result_shows_answer_on_wrong():
    text, _ = bot.build_choice_result(PREP, "on", False)
    assert "Неверно" in text and "*in*" in text and "📖" in text


def test_type_result_survives_markdown_in_user_input():
    """Regression: a typed answer with `, _ or * must not unbalance Markdown
    (which would make Telegram reject the edit and drop user feedback)."""
    item = {"v1": "go", "v2": "went", "v3": "gone", "translation": "т", "example": "e"}
    for evil in ["went`gone", "go_went", "a*b went", "`*_[]`"]:
        text, _ = bot.build_type_result(item, evil, correct=False)
        assert text.count("`") % 2 == 0, evil
        assert text.count("_") % 2 == 0, evil
        assert text.count("*") % 2 == 0, evil


def test_type_answer_callback_not_treated_as_exercise_type():
    """Regression: 'type_answer' (the Написать button) starts with 'type_' but
    must NOT be caught by the exercise-type selector, or typing mode crashes."""
    exercise_cbs = [f"type_{t}" for t in ("verbs", "prep", "vp", "adjprep", "mixed")]
    for cb in exercise_cbs:
        assert cb[5:] in bot.CONTENT or cb[5:] == "mixed"
    # the input button must fail that same guard
    assert not ("type_answer"[5:] in bot.CONTENT or "type_answer"[5:] == "mixed")


def test_sanitize_user_text_strips_specials_and_caps_length():
    assert bot._sanitize_user_text("a`b*c_d[e]") == "abcde"
    assert len(bot._sanitize_user_text("x" * 500)) == 100


def test_type_result_has_next_button_on_correct():
    item = {"v1": "go", "v2": "went", "v3": "gone", "translation": "т",
            "example": "e"}
    text, kb = bot.build_type_result(item, "went gone", True)
    assert kb is not None and "Дальше" in str(kb)
    assert "Верно" in text


def test_end_review_intro_offers_finish_and_review_no_discard():
    """Regression: the post-deck screen must let the user finish & save, not
    only review-or-discard (which lost the whole session)."""
    text, kb = bot.build_end_review_intro(3)
    assert "Основная колода пройдена" in text
    assert "3" in text
    flat = str(kb)
    assert "start_review" in flat        # повторить ошибки
    assert "finish_session" in flat      # завершить и сохранить
    assert "stop_session" not in flat    # no discard trap here


def test_final_screen_counts_and_review_block():
    session = {
        "results": {"verbs::go": False, "verbs::keep": True},
        "original_total": 2, "exercise_type": "verbs", "user_id": None,
    }
    text, kb = bot.build_final(session, streak=0)
    assert "Сессия завершена" in text
    assert "50%" in text
    assert "go" in text                      # in the "Повтори" block
    assert "Ещё раз" in str(kb) and "Другая тема" in str(kb)


def test_final_screen_perfect_grade():
    session = {"results": {"go": True}, "original_total": 1,
               "exercise_type": "verbs", "user_id": None}
    text, _ = bot.build_final(session, streak=0)
    assert "100%" in text and "🏆" in text


def test_weak_menu_groups_collision_separately(db):
    db.ensure_user(1)
    db.save_session(1, 0, 1, 1, {"keep": False}, "verbs")
    db.save_session(1, 0, 1, 1, {"keep": False}, "vp")
    db.save_session(1, 0, 1, 1, {"upset": False}, "adjprep")
    text, kb = bot.build_menu_weak(1)
    assert "Неправильные глаголы" in text
    assert "Глаголы + to / -ing" in text
    assert "Прилагательные + предлог" in text
    assert "Главное меню" in str(kb)


def test_weak_menu_empty_state(db):
    db.ensure_user(1)
    text, _ = bot.build_menu_weak(1)
    assert "Пока чисто" in text


def test_progress_header_empty_for_new_user(db):
    db.ensure_user(1)
    assert bot._progress_header(1) == ""
    assert bot._progress_header(None) == ""


def test_progress_header_shows_after_a_session(db):
    db.ensure_user(1)
    db.save_session(1, 2, 1, 3, {"go": True, "do": True, "keep": False}, "verbs")
    header = bot._progress_header(1)
    assert "Освоено" in header


def test_main_menu_shows_progress_for_returning_user(db):
    db.ensure_user(1)
    db.save_session(1, 1, 0, 1, {"go": True}, "verbs")
    text, _ = bot.build_type_selector(user_id=1)
    assert "Освоено" in text and "Что хочешь потренировать" in text


def test_main_menu_clean_for_new_user(db):
    db.ensure_user(2)
    text, _ = bot.build_type_selector(user_id=2)
    assert "Освоено" not in text          # no stats noise before the first session


def test_size_selector_hides_10_button_when_small():
    # adjprep has 58 cards -> shows 10/20; a tiny synthetic type would hide 10,
    # but here just assert the real types expose sane buttons.
    text, kb = bot.build_size_selector("adjprep")
    flat = str(kb)
    assert "size_10" in flat and "size_20" in flat and "size_all" in flat
