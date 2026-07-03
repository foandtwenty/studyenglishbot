"""check_typed_answer: forward/reverse matching and typo forgiveness."""
import bot

GO   = {"v1": "go",    "v2": "went",      "v3": "gone",    "translation": "т", "example": "e"}
BE   = {"v1": "be",    "v2": "was/were",  "v3": "been",    "translation": "т", "example": "e"}
CUT  = {"v1": "cut",   "v2": "cut",       "v3": "cut",     "translation": "т", "example": "e"}
SIT  = {"v1": "sit",   "v2": "sat",       "v3": "sat",     "translation": "т", "example": "e"}
COME = {"v1": "come",  "v2": "came",      "v3": "come",    "translation": "т", "example": "e"}
BUY  = {"v1": "bring", "v2": "brought",   "v3": "brought", "translation": "т", "example": "e"}


def check(item, raw, reverse=False):
    return bot.check_typed_answer(item, raw, reverse=reverse)


# ─── forward: exact ───────────────────────────────────────────────────────────

def test_forward_exact():
    assert check(GO, "went gone") == (True, False)
    assert check(GO, "went, gone.") == (True, False)          # punctuation
    assert check(BE, "was been") == (True, False)
    assert check(BE, "was/were been") == (True, False)        # canonical multi-V2
    assert check(CUT, "cut") == (True, False)
    assert check(CUT, "cut cut") == (True, False)


def test_forward_wrong():
    assert check(GO, "went blah gone")[0] is False            # garbage token
    assert check(GO, "went")[0] is False                      # V3 missing
    assert check(SIT, "sat sitten")[0] is False               # invalid extra form
    assert check(GO, "")[0] is False


# ─── typo forgiveness ─────────────────────────────────────────────────────────

def test_typo_accepted_for_long_forms():
    assert check(BUY, "brougth brought") == (True, True)      # transposition
    assert check(BUY, "brought brouht") == (True, True)       # missing letter
    assert check(BUY, "brought brought") == (True, False)     # clean stays clean


def test_typo_rejected_for_short_forms():
    # went (4 letters) gets no tolerance: "wend" is a different word, not a slip
    assert check(GO, "wend gone")[0] is False
    assert check(CUT, "cot")[0] is False


def test_form_confusion_is_not_a_typo():
    # came is exactly another form of come — swapping forms must stay wrong
    assert check(COME, "came came")[0] is False
    assert check(COME, "come come")[0] is False
    assert check(COME, "came come") == (True, False)


# ─── reverse (RU→EN) ─────────────────────────────────────────────────────────

def test_reverse_full_answer():
    assert check(GO, "go went gone", reverse=True) == (True, False)
    assert check(BE, "be was been", reverse=True) == (True, False)


def test_reverse_requires_v1_and_forms():
    assert check(GO, "went gone", reverse=True)[0] is False   # V1 missing
    assert check(GO, "go", reverse=True)[0] is False          # forms missing
    assert check(GO, "come went gone", reverse=True)[0] is False   # wrong verb


def test_reverse_same_form_verbs():
    assert check(CUT, "cut", reverse=True) == (True, False)   # all-identical: enough
    assert check(CUT, "cut cut cut", reverse=True) == (True, False)
    assert check(SIT, "sit sat", reverse=True) == (True, False)
    assert check(SIT, "sit", reverse=True)[0] is False        # form still required


def test_reverse_typo_in_verb():
    assert check(BUY, "brign brought brought", reverse=True) == (True, True)


# ─── _edit_distance_1 ────────────────────────────────────────────────────────

def test_edit_distance_one():
    assert bot._edit_distance_1("brought", "brougth") is True   # adjacent swap
    assert bot._edit_distance_1("brought", "brouht") is True    # deletion
    assert bot._edit_distance_1("brought", "broughtt") is True  # insertion
    assert bot._edit_distance_1("brought", "braught") is True   # substitution
    assert bot._edit_distance_1("go", "go") is False            # identical
    assert bot._edit_distance_1("go", "gone") is False          # 2 apart
