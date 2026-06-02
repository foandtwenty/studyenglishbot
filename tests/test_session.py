"""Session mechanics: queue, marking, spaced-repetition re-insertion, and the
end-of-deck review accumulation."""
import bot

A = {"v1": "a", "v2": "a2", "v3": "a3", "translation": "t", "example": "e"}
B = {"v1": "b", "v2": "b2", "v3": "b3", "translation": "t", "example": "e"}
C = {"v1": "c", "v2": "c2", "v3": "c3", "translation": "t", "example": "e"}


def test_new_session_with_deck_preserves_order_and_total():
    s = bot.new_session("verbs", deck=[A, B, C])
    assert [bot.item_id(x) for x in s["queue"]] == ["a", "b", "c"]
    assert s["original_total"] == 3
    assert s["pos"] == 0 and s["phase"] == "main"
    assert s["results"] == {} and s["first_shown"] == set()


def test_new_session_with_size_truncates():
    s = bot.new_session("verbs", size=5)
    assert s["original_total"] == 5
    assert len(s["queue"]) == 5


def test_current_item_and_exhaustion():
    s = bot.new_session("verbs", deck=[A])
    assert bot.current_item(s) is A
    bot.advance(s)
    assert bot.current_item(s) is None


def test_mark_known():
    s = bot.new_session("verbs", deck=[A])
    bot.mark_known(s, A)
    assert s["results"]["verbs::a"] is True
    assert s["end_review"] == [] and s["review_buffer"] == []


def test_mark_unknown_queues_for_review_and_end(monkeypatch):
    monkeypatch.setattr(bot.random, "randint", lambda lo, hi: 2)
    s = bot.new_session("verbs", deck=[A, B, C])
    bot.mark_unknown(s, A)
    assert s["results"]["verbs::a"] is False
    assert [bot.item_id(i) for i, _ in s["review_buffer"]] == ["a"]
    assert [bot.item_id(i) for i in s["end_review"]] == ["a"]


def test_mark_unknown_dedupes(monkeypatch):
    monkeypatch.setattr(bot.random, "randint", lambda lo, hi: 2)
    s = bot.new_session("verbs", deck=[A, B])
    bot.mark_unknown(s, A)
    bot.mark_unknown(s, A)
    assert len(s["review_buffer"]) == 1
    assert len(s["end_review"]) == 1


def test_review_item_reinserted_after_countdown(monkeypatch):
    monkeypatch.setattr(bot.random, "randint", lambda lo, hi: 2)
    s = bot.new_session("verbs", deck=[A, B, C])
    bot.mark_unknown(s, A)          # countdown 2
    bot.advance(s)                  # pos1, countdown -> 1
    assert all(bot.item_id(i) != "a" for i in s["queue"][s["pos"]:])
    bot.advance(s)                  # pos2, countdown -> 0 => reinsert at pos
    assert bot.item_id(s["queue"][s["pos"]]) == "a"


def test_mark_unknown_in_review_phase_does_not_requeue():
    s = bot.new_session("verbs", deck=[A])
    s["phase"] = "end_review"
    bot.mark_unknown(s, A)
    assert s["results"]["verbs::a"] is False
    assert s["review_buffer"] == [] and s["end_review"] == []


def test_full_pass_reaches_total_with_no_lost_cards():
    """Regression: duplicate item_ids used to make first_shown never reach
    original_total. A clean deck must converge exactly."""
    s = bot.new_session("adjprep", user_id=1)
    total = s["original_total"]
    guard = 0
    while bot.current_item(s) is not None and guard < 1000:
        it = bot.current_item(s)
        s["first_shown"].add(bot.item_id(it))
        bot.mark_known(s, it)
        bot.advance(s)
        guard += 1
    assert len(s["first_shown"]) == total
    assert len(s["results"]) == total


def test_new_session_has_version():
    s = bot.new_session("verbs", deck=[A])
    assert s["_v"] == bot.SESSION_VERSION


def test_normalize_backfills_missing_keys():
    # an "old" session missing keys added later
    old = {"queue": [A], "results": {}, "pos": 0, "exercise_type": "verbs"}
    out = bot.normalize_session(old)
    assert out is old                      # normalized in place
    for k in ("phase", "first_shown", "review_buffer", "end_review",
              "awaiting_input", "user_id", "original_total"):
        assert k in out
    assert out["_v"] == bot.SESSION_VERSION


def test_normalize_drops_structurally_broken():
    assert bot.normalize_session(None) is None
    assert bot.normalize_session({"foo": 1}) is None          # no queue/results
    assert bot.normalize_session("not a dict") is None


def test_normalize_does_not_share_mutable_defaults():
    a = bot.normalize_session({"queue": [], "results": {}})
    b = bot.normalize_session({"queue": [], "results": {}})
    a["first_shown"].add("x")
    assert b["first_shown"] == set()       # independent objects
