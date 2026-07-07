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


def test_parse_callback_separates_dynamic_from_static():
    """Routing is collision-proof by construction: dynamic families parse to
    (prefix, arg); static actions (incl. the old foot-gun 'type_answer') parse
    to (data, None) and can never be mistaken for an exercise pick."""
    assert bot.parse_callback("pick:verbs") == ("pick", "verbs")
    assert bot.parse_callback("size:30")    == ("size", "30")
    assert bot.parse_callback("ans:of")     == ("ans", "of")
    assert bot.parse_callback("type_answer") == ("type_answer", None)
    assert bot.parse_callback("stop_session") == ("stop_session", None)
    # a static action never collides with a dynamic family
    action, arg = bot.parse_callback("type_answer")
    assert action not in bot.DYNAMIC_PREFIXES


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
    assert "Тренировка завершена" in text
    assert "50%" in text
    assert "go" in text                      # in the "Повтори" block
    assert "Ещё раз" in str(kb) and "В меню" in str(kb)


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
    assert "menu_profile" in str(kb)        # back goes to the profile sub-screen


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
    text, kb = bot.build_type_selector(user_id=1)
    assert "Освоено" in text                       # progress header shown
    assert "menu_topics" in str(kb)                # themes are one tap away


def test_main_menu_clean_for_new_user(db):
    db.ensure_user(2)
    text, _ = bot.build_type_selector(user_id=2)
    assert "Освоено" not in text          # no stats noise before the first session


def test_mixed_size_selector_caps_and_has_no_all():
    _, kb = bot.build_size_selector("mixed")
    flat = str(kb)
    assert "size:10" in flat and "size:20" in flat and "size:30" in flat
    assert "size:all" not in flat            # no «Все 249»


def test_type_mode_applies_to_verb_cards_in_any_deck():
    """Input mode applies to a verb card regardless of the deck (pure verbs,
    mixed, or review) — only non-verb cards stay button-only."""
    verb = {"v1": "go", "v2": "went", "v3": "gone", "translation": "идти", "example": "e"}
    prep = {"sentence": "X {?} Y.", "answer": "in", "translation": "т", "rule": "r"}
    # the show_card gating expression: tm = type_mode and item_type == "verbs"
    assert (True and bot.item_type(verb) == "verbs") is True
    assert (True and bot.item_type(prep) == "verbs") is False


def test_single_type_selector_shows_levels_not_counts():
    text, kb = bot.build_size_selector("adjprep")
    flat = str(kb)
    assert "С чего начнём" in text
    assert "lvl:1" in flat and "lvl:2" in flat and "lvl:3" in flat and "lvl:all" in flat
    assert "size:10" not in flat                # single types are level-based now
    # button labels show the per-level counts
    assert "🟢 Базовый" in flat and "🔴 Продвинутый" in flat


def test_level_decks_partition_each_type():
    for ex in ("verbs", "prep", "vp", "adjprep"):
        sizes = [len(bot._level_deck(ex, lvl)) for lvl in (1, 2, 3)]
        assert sum(sizes) == len(bot.CONTENT[ex])      # every card has exactly one level
        assert all(s > 0 for s in sizes)               # no empty tier
