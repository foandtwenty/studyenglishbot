"""Content integrity. These guard the exercise datasets against the classes of
bug that only surface at runtime (duplicate ids, cross-set collisions, a
multiple-choice card whose correct answer isn't among its options)."""
import bot
from verbs import VERBS
from prepositions import PREPOSITIONS
from verb_patterns import VERB_PATTERNS
from adj_preps import ADJ_PREPS

ALL = {
    "verbs": VERBS, "prep": PREPOSITIONS,
    "vp": VERB_PATTERNS, "adjprep": ADJ_PREPS,
}


def test_no_duplicate_ids_within_each_set():
    for name, data in ALL.items():
        ids = [bot.item_id(x) for x in data]
        dups = sorted({i for i in ids if ids.count(i) > 1})
        assert not dups, f"{name}: дубли item_id {dups}"


def test_no_cross_set_id_collisions_break_stats():
    """verb_stats keys are namespaced, so cross-set collisions are allowed —
    but only because _stat_key keeps them apart. Assert the namespacing makes
    every (type, id) pair unique even where bare ids collide."""
    keys = []
    for name, data in ALL.items():
        keys += [bot._stat_key(name, bot.item_id(x)) for x in data]
    assert len(keys) == len(set(keys))


def test_adjprep_options_contain_correct_answer():
    for a in ADJ_PREPS:
        assert a["preposition"] in a["options"], a["adjective"]
        assert len(a["options"]) == 3, a["adjective"]


def test_vp_patterns_valid():
    for v in VERB_PATTERNS:
        assert v["pattern"] in ("ing", "to"), v["verb"]


def test_prep_answers_valid_and_have_placeholder():
    for p in PREPOSITIONS:
        assert p["answer"] in ("in", "on", "at"), p["sentence"]
        assert "{?}" in p["sentence"], p["sentence"]


def test_required_fields_present():
    for v in VERBS:
        assert {"v1", "v2", "v3", "translation", "example"} <= v.keys()
    for p in PREPOSITIONS:
        assert {"sentence", "answer", "translation", "rule"} <= p.keys()
    for v in VERB_PATTERNS:
        assert {"verb", "pattern", "translation", "example", "rule"} <= v.keys()
    for a in ADJ_PREPS:
        assert {"adjective", "preposition", "translation", "example", "options"} <= a.keys()


def test_markdown_balanced_in_user_text():
    """Legacy Markdown breaks on unbalanced * _ ` — scan all rendered fields."""
    def balanced(s):
        return s.count("*") % 2 == 0 and s.count("_") % 2 == 0 and s.count("`") % 2 == 0

    fields = []
    for v in VERBS:
        fields += [v["example"], v["translation"], v.get("note", "")]
    for p in PREPOSITIONS:
        fields += [p["rule"], p["translation"]]
    for v in VERB_PATTERNS:
        fields += [v["rule"], v["example"]]
    for a in ADJ_PREPS:
        fields += [a["rule"], a["example"]]
    bad = [s for s in fields if not balanced(s)]
    assert not bad, bad
