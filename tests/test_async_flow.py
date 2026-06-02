"""Async rendering paths driven through a FakeBot (no network)."""
import asyncio

import bot

A = {"v1": "a", "v2": "a2", "v3": "a3", "translation": "перевод", "example": "e"}
B = {"v1": "b", "v2": "b2", "v3": "b3", "translation": "перевод", "example": "e"}


def run(coro):
    return asyncio.run(coro)


def test_show_card_renders_and_marks_first_shown(fake_bot, make_session):
    s = make_session("verbs", [A, B])
    run(bot.show_card(1, s, fake_bot))
    assert "a" in fake_bot.last_text
    assert "verbs::a" in s["first_shown"]     # marked only after a successful edit


def test_show_card_on_exhausted_deck_finalizes(fake_bot, make_session, db):
    db.ensure_user(1)
    s = make_session("verbs", [A])
    bot.mark_known(s, A)
    s["pos"] = len(s["queue"])                # deck exhausted, no end_review
    run(bot.show_card(1, s, fake_bot))
    assert "Сессия завершена" in fake_bot.last_text


def test_show_results_transitions_to_end_review(fake_bot, make_session, monkeypatch):
    monkeypatch.setattr(bot.random, "randint", lambda lo, hi: 2)
    s = make_session("verbs", [A, B])
    bot.mark_unknown(s, A)
    bot.mark_unknown(s, B)
    s["pos"] = len(s["queue"])                # main deck consumed
    run(bot.show_results(1, s, fake_bot))
    assert s["phase"] == "end_review"
    assert "Основная колода пройдена" in fake_bot.last_text


def test_show_results_finalizes_from_end_review_phase(fake_bot, make_session, db):
    """Stopping/finishing during the review phase must save, not discard."""
    db.ensure_user(1)
    s = make_session("verbs", [A, B])
    bot.mark_unknown(s, A)
    bot.mark_known(s, B)
    s["phase"] = "end_review"
    s["queue"] = []
    s["pos"] = 0
    run(bot.show_results(1, s, fake_bot))
    assert "Сессия завершена" in fake_bot.last_text
    assert db.get_history(1)[0]["total"] == 2     # persisted


def test_show_results_finalizes_and_persists(fake_bot, make_session, db):
    db.ensure_user(1)
    s = make_session("verbs", [A])
    bot.mark_known(s, A)
    s["pos"] = len(s["queue"])
    run(bot.show_results(1, s, fake_bot))
    assert "Сессия завершена" in fake_bot.last_text
    # a session row was written
    assert db.get_history(1)[0]["total"] == 1
    # known answer recorded under namespaced key
    assert "verbs::a" not in db.get_weak_ids(1)   # it was correct -> not weak
