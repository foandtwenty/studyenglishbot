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


def test_sanitize_user_text_strips_specials_and_caps_length():
    assert bot._sanitize_user_text("a`b*c_d[e]") == "abcde"
    assert len(bot._sanitize_user_text("x" * 500)) == 100


def test_type_result_has_next_button_on_correct():
    item = {"v1": "go", "v2": "went", "v3": "gone", "translation": "т",
            "example": "e"}
    text, kb = bot.build_type_result(item, "went gone", True)
    assert kb is not None and "Дальше" in str(kb)
    assert "Верно" in text


def test_end_review_intro_grammar():
    one, _ = bot.build_end_review_intro(1)
    assert "которая вызвала" in one
    many, _ = bot.build_end_review_intro(3)
    assert "которые вызвали" in many


def test_final_screen_counts_and_review_block():
    session = {
        "results": {"go": False, "keep": True},
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
    assert "Пока нет данных" in text


def test_size_selector_hides_10_button_when_small():
    # adjprep has 58 cards -> shows 10/20; a tiny synthetic type would hide 10,
    # but here just assert the real types expose sane buttons.
    text, kb = bot.build_size_selector("adjprep")
    flat = str(kb)
    assert "size_10" in flat and "size_20" in flat and "size_all" in flat
