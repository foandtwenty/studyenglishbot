"""Pure helper functions: pluralization, ids, verb forms, progress bar."""
import bot


def test_card_plural():
    assert bot._card_plural(1) == "карточку"
    assert bot._card_plural(2) == "карточки"
    assert bot._card_plural(4) == "карточки"
    assert bot._card_plural(5) == "карточек"
    assert bot._card_plural(11) == "карточек"   # 11 — исключение
    assert bot._card_plural(21) == "карточку"
    assert bot._card_plural(22) == "карточки"
    assert bot._card_plural(25) == "карточек"


def test_streak_label():
    assert bot._streak_label(1) == "день"
    assert bot._streak_label(2) == "дня"
    assert bot._streak_label(5) == "дней"
    assert bot._streak_label(11) == "дней"
    assert bot._streak_label(21) == "день"


def test_item_id_per_type():
    assert bot.item_id({"v1": "go"}) == "go"
    assert bot.item_id({"verb": "want"}) == "want"
    assert bot.item_id({"adjective": "afraid"}) == "afraid"
    assert bot.item_id({"sentence": "X {?} Y."}) == "X {?} Y."


def test_stat_key():
    assert bot._stat_key("verbs", "go") == "verbs::go"
    assert bot._stat_key("vp", "keep") == "vp::keep"


def test_vp_display_strips_dual_annotation():
    assert bot._vp_display({"verb": "stop  (прекратить)"}) == "stop"
    assert bot._vp_display({"verb": "want"}) == "want"


def test_verb_forms_text():
    assert "одинаковые" in bot._verb_forms_text({"v1": "cut", "v2": "cut", "v3": "cut"})
    assert "V2 = V3" in bot._verb_forms_text({"v1": "buy", "v2": "bought", "v3": "bought"})
    out = bot._verb_forms_text({"v1": "go", "v2": "went", "v3": "gone"})
    assert "went" in out and "gone" in out


def test_progress_bar_counts():
    session = {
        "phase": "main", "original_total": 10,
        "results": {f"k{i}": True for i in range(5)} | {f"u{i}": False for i in range(3)},
    }
    bar = bot._progress_bar(session)
    assert bar.count("🟩") == 5
    assert bar.count("🟥") == 3
    assert bar.count("⬜️") == 2


def test_progress_bar_empty_when_no_total():
    assert bot._progress_bar({"phase": "main", "original_total": 0, "results": {}}) == ""
