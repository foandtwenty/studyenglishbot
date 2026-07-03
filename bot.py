import os
import re
import random
import logging
import datetime as dt

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, PicklePersistence,
)
from telegram.error import BadRequest

from verbs import VERBS
from prepositions import PREPOSITIONS
from verb_patterns import VERB_PATTERNS
from adj_preps import ADJ_PREPS
from levels import LEVELS
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs every request URL at INFO — and the URL embeds the bot token.
# Keep it at WARNING so the token never lands in logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

VERBS_BY_V1  = {v["v1"]:        v for v in VERBS}
VP_BY_VERB   = {v["verb"]:      v for v in VERB_PATTERNS}
PREP_BY_SENT = {p["sentence"]:  p for p in PREPOSITIONS}
ADJ_BY_ADJ   = {a["adjective"]: a for a in ADJ_PREPS}

CONTENT = {
    "verbs":   VERBS,
    "prep":    PREPOSITIONS,
    "vp":      VERB_PATTERNS,
    "adjprep": ADJ_PREPS,
}

# Session phases (kept as named constants to avoid stringly-typed comparisons).
PHASE_MAIN = "main"
PHASE_END_REVIEW = "end_review"

TYPE_EMOJI = {"verbs": "🔤", "prep": "📍", "vp": "➕", "adjprep": "🔗",
              "mixed": "🎲", "review": "🔔"}
TYPE_LABEL = {
    "verbs":   "Неправильные глаголы",
    "prep":    "Предлоги in / on / at",
    "vp":      "Глаголы + to / -ing",
    "adjprep": "Прилагательные + предлог",
    "mixed":   "Всё вперемешку",
    "review":  "Тренировка дня",
}

HELP_TEXT = (
    "📚 *Study English Bot*\n\n"
    "*Типы тренировок:*\n"
    "🔤 Неправильные глаголы — вспомни V2 и V3\n"
    "📍 Предлоги — in, on или at?\n"
    "➕ Глаголы + to / -ing\n"
    "🔗 Прилагательные + предлог (afraid of...)\n\n"
    "*Как работает:*\n"
    "Сначала вспоминаешь сам, затем смотришь ответ и честно оцениваешь.\n"
    "Ошибочные карточки возвращаются через 2–3 хода и снова в конце.\n\n"
    "*Интервальное повторение:*\n"
    "🔔 *Повторить* собирает карточки, которым пора освежиться: сложные "
    "(где ты ошибался) идут первыми и возвращаются часто, а верные — по "
    "растущим интервалам (1, 2, 4, 7… дней), чтобы не забылись.\n\n"
    "*В главном меню:*\n"
    "🎲 Всё вперемешку — карточки всех типов сразу\n"
    "📊 Статистика · 📋 Сложные — список твоих ошибок\n"
    "📈 История · ❓ Помощь\n\n"
    "*Перед стартом:*\n"
    "Выбери уровень — 🟢 Базовый (самые частые), 🟡 Средний, 🔴 Продвинутый "
    "— или «📚 Все».\n"
    "🎯 Только ошибки — тренировать лишь сложные карточки\n"
    "✏️ Режим ввода — печатай формы вместо кнопок (проверка выученного)\n\n"
    "*На карточке глагола:*\n"
    "💡 Подсказка — первые буквы и длина V2/V3\n"
    "В режиме ввода просто отправь V2 и V3 в чат; «❓ Не помню» покажет ответ"
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def item_id(item: dict) -> str:
    return item.get("v1") or item.get("verb") or item.get("adjective") or item.get("sentence", "?")


def item_type(item: dict) -> str:
    if "v1" in item:        return "verbs"
    if "verb" in item:      return "vp"
    if "adjective" in item: return "adjprep"
    return "prep"


def card_key(item: dict) -> str:
    """Stable namespaced key used for results, scheduling and dedup. Works for
    mixed-type decks where a bare item_id could collide across exercises."""
    return _stat_key(item_type(item), item_id(item))


# Difficulty tiers: 1=Базовый, 2=Средний, 3=Продвинутый.
LEVEL_LABEL = {1: "🟢 Базовый", 2: "🟡 Средний", 3: "🔴 Продвинутый"}


def item_level(item: dict) -> int:
    return LEVELS.get(item_type(item), {}).get(item_id(item), 2)


def _level_deck(exercise_type: str, level: int) -> list:
    return [i for i in CONTENT[exercise_type] if item_level(i) == level]


# ─── Callback routing ───────────────────────────────────────────────────────
# Dynamic callback families carry an argument as "prefix:arg"; everything else
# is a static action (arg=None). Separate namespaces make collisions — like the
# old `type_answer` being swallowed by the `type_<exercise>` matcher —
# impossible by construction, and make routing a pure, unit-testable function.
DYNAMIC_PREFIXES = frozenset({"pick", "size", "ans", "lvl"})


def parse_callback(data: str) -> tuple[str, str | None]:
    prefix, sep, arg = data.partition(":")
    if sep and prefix in DYNAMIC_PREFIXES:
        return prefix, arg
    return data, None


def _streak_label(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:                  return "день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return "дня"
    return "дней"


def _card_plural(n: int) -> str:
    """Accusative — for «знаю N карточек», «учу N карточек»."""
    if n % 10 == 1 and n % 100 != 11:                  return "карточку"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return "карточки"
    return "карточек"


def _card_plural_nom(n: int) -> str:
    """Nominative — for labels like «тема · N карточек», «К повторению: N»."""
    if n % 10 == 1 and n % 100 != 11:                  return "карточка"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return "карточки"
    return "карточек"


def _verb_forms_text(item: dict) -> str:
    v1, v2, v3 = item["v1"], item["v2"], item["v3"]
    if v1 == v2 == v3:
        return f"Все формы одинаковые: `{v1}`"
    if v2 == v3:
        return f"*V2 = V3:* `{v2}`"
    return f"*V2:* `{v2}`\n*V3:* `{v3}`"


def _vp_display(item: dict) -> str:
    return item["verb"].split("  ")[0].strip()


def _norm_forms(s: str) -> list[str]:
    """Accepted spellings of a verb form, e.g. 'was/were' -> ['was','were'].
    Strips leading/trailing punctuation so 'went, gone.' matches correctly."""
    tokens = s.lower().replace("/", " ").split()
    stripped = (re.sub(r"^[^\w]+|[^\w]+$", "", t) for t in tokens)
    return [t for t in stripped if t]


def _sanitize_user_text(s: str) -> str:
    """Strip Markdown-special chars from echoed user input so a typed answer
    like ``went`gone`` can't break parse_mode=Markdown (silent BadRequest)."""
    return re.sub(r"[`*_\[\]]", "", s)[:100]


def _stat_key(exercise_type: str, iid: str) -> str:
    return f"{exercise_type}::{iid}"


def _build_weak_deck(exercise_type: str, user_id: int) -> list:
    weak_ids = db.get_weak_ids(user_id)
    deck = []
    for item in CONTENT[exercise_type]:
        key = _stat_key(exercise_type, item_id(item))
        if key in weak_ids:
            deck.append((item, weak_ids[key]))
    deck.sort(key=lambda x: x[1], reverse=True)
    return [item for item, _ in deck]


def _mixed_pool() -> list:
    """All cards from every exercise, for interleaved practice."""
    return [item for lst in CONTENT.values() for item in lst]


ALL_CARD_KEYS = frozenset(card_key(it) for it in _mixed_pool())

# Daily review is capped so it never balloons into a burnout grind. The hardest
# cards come first, so a cap always keeps the most important ones; the rest stay
# due and surface in the next session. New cards are introduced a few per day so
# the daily training also grows vocabulary without overwhelming.
REVIEW_CAP  = 30
NEW_PER_DAY = 7


def _build_review_deck(user_id: int) -> list:
    """The day's review: due cards across all types, hardest-first (most past
    errors), shuffled within each error tier, capped at REVIEW_CAP."""
    due  = set(db.get_due_ids(user_id))
    weak = db.get_weak_ids(user_id)                 # {card_key: unknown_count}
    deck = [item for item in _mixed_pool() if card_key(item) in due]
    random.shuffle(deck)                            # randomize within equal tiers
    deck.sort(key=lambda it: weak.get(card_key(it), 0), reverse=True)
    return deck[:REVIEW_CAP]


def _build_new_deck(user_id: int, limit: int = NEW_PER_DAY) -> list:
    """Brand-new (never-answered) cards for the daily budget — easiest level
    first, types mixed within a level."""
    seen = db.get_seen_keys(user_id)
    fresh = [it for it in _mixed_pool() if card_key(it) not in seen]
    random.shuffle(fresh)                           # mix types within a level
    fresh.sort(key=item_level)                      # Базовый first
    return fresh[:limit]


def _build_daily_deck(user_id: int) -> list:
    """«Тренировка дня» = capped due reviews (hardest first) + a few new cards."""
    new_today  = db.get_new_today_count(user_id)
    new_budget = max(0, NEW_PER_DAY - new_today)
    return _build_review_deck(user_id) + _build_new_deck(user_id, limit=new_budget)


def _daily_counts(user_id: int | None) -> tuple[int, int]:
    """(reviews_due_capped, new_available_capped) for the menu badge/text."""
    if not user_id:
        return 0, 0
    try:
        reviews      = min(_due_count(user_id), REVIEW_CAP)
        new_today    = db.get_new_today_count(user_id)
        new_budget   = max(0, NEW_PER_DAY - new_today)
        unseen       = len(ALL_CARD_KEYS - db.get_seen_keys(user_id))
        new          = min(unseen, new_budget)
        return reviews, new
    except Exception:
        logger.exception("daily counts failed for user %s", user_id)
        return 0, 0


def _due_count(user_id: int | None) -> int:
    """Due cards that still exist in the current content — matches what tapping
    «К повторению» actually opens (db.get_due_count may include orphaned keys
    left over from content edits)."""
    if not user_id:
        return 0
    try:
        return len(set(db.get_due_ids(user_id)) & ALL_CARD_KEYS)
    except Exception:
        logger.exception("due count failed for user %s", user_id)
        return 0


def _progress_header(user_id: int | None) -> str:
    """Compact at-a-glance progress for the main menu. Empty for new users."""
    if not user_id:
        return ""
    try:
        streak = db.get_streak(user_id)
        lt     = db.get_lifetime_stats(user_id)
    except Exception:
        return ""
    if lt["sessions"] == 0:
        return ""
    lines = []
    if streak:
        lines.append(f"🔥 Серия: *{streak} {_streak_label(streak)}*")
    second = f"Освоено: *{lt['mastered']}*"
    if lt["learning"]:
        second += f"  ·  Изучается: *{lt['learning']}*"
    lines.append(second)
    return "\n".join(lines)


# ─── Session ──────────────────────────────────────────────────────────────────

# Bump when the session dict shape changes incompatibly. Persisted sessions
# (PicklePersistence) from older code are normalized on load; structurally
# broken ones are dropped so a redeploy never crashes an active user.
SESSION_VERSION = 2

_SESSION_DEFAULTS: dict = {
    "exercise_type":  "verbs",
    "pos":            0,
    "first_shown":    set(),
    "review_buffer":  [],
    "end_review":     [],
    "phase":          PHASE_MAIN,
    "message_id":     None,
    "awaiting_input": False,
    "user_id":        None,
    "ever_wrong":     set(),       # cards missed at any point this session
    "hint_used":      set(),       # cards where a hint was used (type mode)
}


def new_session(exercise_type: str, size: int | None = None,
                user_id: int | None = None, deck: list | None = None) -> dict:
    if deck is not None:
        d = deck.copy()
    else:
        d = _mixed_pool() if exercise_type == "mixed" else CONTENT[exercise_type].copy()
        random.shuffle(d)
        if size:
            d = d[:size]
    return {
        "exercise_type":  exercise_type,
        "queue":          d,
        "pos":            0,
        "original_total": len(d),
        "first_shown":    set(),
        "results":        {},
        "review_buffer":  [],
        "end_review":     [],
        "phase":          PHASE_MAIN,
        "message_id":     None,
        "awaiting_input": False,
        "user_id":        user_id,
        "ever_wrong":     set(),
        "hint_used":      set(),
        "_v":             SESSION_VERSION,
    }


def normalize_session(s: object) -> dict | None:
    """Backfill defaults onto a possibly-older (unpickled) session in place.
    Returns None if it's structurally unusable — caller then treats it as no
    active session instead of crashing."""
    if not isinstance(s, dict) or "queue" not in s or "results" not in s:
        return None
    for k, v in _SESSION_DEFAULTS.items():
        if k not in s:
            s[k] = v.copy() if isinstance(v, (set, list, dict)) else v
    s.setdefault("original_total", len(s["queue"]))
    s["_v"] = SESSION_VERSION
    return s


def current_item(session: dict) -> dict | None:
    q, p = session["queue"], session["pos"]
    return q[p] if p < len(q) else None


def _resume_info(context) -> tuple[int, int] | None:
    """(theme_label, done, total) for an unfinished session that can be resumed,
    else None (a finished session has no current card)."""
    s = context.user_data.get("session")
    if s and current_item(s) is not None:
        label = TYPE_LABEL.get(s.get("exercise_type", ""), "")
        return label, len(s["first_shown"]), s["original_total"]
    return None


# ─── Progress line ────────────────────────────────────────────────────────────

def progress_line(session: dict, item: dict) -> str:
    if session["phase"] == PHASE_END_REVIEW:
        pos   = session["pos"] + 1
        total = len(session["queue"])
        return f"_Повторение {pos} / {total}_"

    key    = card_key(item)
    is_new = key not in session["first_shown"]
    done   = len(session["first_shown"])
    total  = session["original_total"]
    counter = f"{done + 1} / {total}" if is_new else f"Повтор · {done} / {total}"
    prefix  = TYPE_EMOJI.get(item_type(item), "📚") if is_new else "🔄"
    return f"{prefix} _{counter}_"


# ─── Selectors ────────────────────────────────────────────────────────────────

def build_type_selector(welcome: bool = False, user_id: int | None = None,
                        resume: tuple[int, int] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    if welcome:
        text = (
            "👋 *Привет! Я Study English Bot.*\n\n"
            "Тренирую английский через флеш-карточки:\n\n"
            "🔤 Неправильные глаголы — вспомни V2 и V3\n"
            "📍 Предлоги — in, on или at?\n"
            "➕ Глаголы + to / -ing\n"
            "🔗 Прилагательные — afraid of, nervous about?\n\n"
            "Ошибочные карточки возвращаются через 2–3 хода "
            "и снова в конце — так слова запоминаются лучше.\n\n"
            "*Выбери тему и начнём!*"
        )
    reviews, new = _daily_counts(user_id)
    daily = reviews + new

    if not welcome:
        # Context lives in the text so the buttons below read clearly.
        info = []
        if resume:
            label, done, total = resume
            info.append(f"⏸ *На паузе:* {label} — осталось {total - done}")
        if daily:
            if reviews and new:
                parts = f"{reviews} на повтор + {new} к изучению"
            elif reviews:
                parts = f"{reviews} {_card_plural_nom(reviews)} на повтор"
            else:
                parts = f"{new} {_card_plural_nom(new)} к изучению"
            info.append(f"🔔 *Тренировка дня:* {parts}")
        header = _progress_header(user_id)
        blocks = []
        if info:   blocks.append("\n".join(info))
        if header: blocks.append(header)
        text = "\n\n".join(blocks) if blocks else "📚 *Что хочешь потренировать?*"

    rows: list[list[InlineKeyboardButton]] = []
    if resume:
        rows.append([InlineKeyboardButton("▶️ Продолжить", callback_data="resume_session")])
    if daily:
        rows.append([InlineKeyboardButton(f"🔔 Тренировка дня ({daily})", callback_data="start_due")])
    rows.append([
        InlineKeyboardButton("📚 Выбрать тему", callback_data="menu_topics"),
        InlineKeyboardButton("⚙️ Профиль",      callback_data="menu_profile"),
    ])
    return text, InlineKeyboardMarkup(rows)


def build_menu_topics() -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤 Неправильные глаголы",     callback_data="pick:verbs")],
        [InlineKeyboardButton("📍 Предлоги in / on / at",    callback_data="pick:prep")],
        [InlineKeyboardButton("➕ Глаголы + to / -ing",      callback_data="pick:vp")],
        [InlineKeyboardButton("🔗 Прилагательные + предлог", callback_data="pick:adjprep")],
        [InlineKeyboardButton("🎲 Всё вперемешку",           callback_data="pick:mixed")],
        [InlineKeyboardButton("← Назад",                     callback_data="back_to_types")],
    ])
    return "📚 *Выбери тему*", kb


def build_menu_profile(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    header = _progress_header(user_id)
    text   = (f"{header}\n\n" if header else "") + "⚙️ *Профиль и прогресс*"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
            InlineKeyboardButton("📋 Сложные",    callback_data="menu_weak"),
        ],
        [
            InlineKeyboardButton("📈 История",    callback_data="menu_history"),
            InlineKeyboardButton("❓ Помощь",     callback_data="menu_help"),
        ],
        [InlineKeyboardButton("← Назад", callback_data="back_to_types")],
    ])
    return text, kb


def build_menu_stats(session: dict | None, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    streak      = db.get_streak(user_id)
    streak_line = f"🔥 Серия: *{streak} {_streak_label(streak)}*\n" if streak else ""
    has_active = bool(session and session["results"])
    try:
        lt = db.get_lifetime_stats(user_id)
        if lt["sessions"] > 0:
            lifetime_block = (
                f"\n🏆 *За всё время*\n"
                f"Карточек пройдено: *{lt['total_cards']}*\n"
                f"Освоено: *{lt['mastered']}*\n"
                f"Изучается: *{lt['learning']}*\n"
                f"Сессий: *{lt['sessions']}*"
            )
        elif has_active:
            lifetime_block = ""      # the current-session block already shows progress
        else:
            lifetime_block = "\n_Заверши первую тренировку — и здесь появится статистика за всё время._"
    except Exception:
        logger.exception("Failed to load lifetime stats for user %s", user_id)
        lifetime_block = ""

    if session and session["results"]:
        results = session["results"]
        known   = sum(1 for v in results.values() if v)
        unknown = sum(1 for v in results.values() if not v)
        studied = len(results)
        total   = session["original_total"]
        ex_type = session["exercise_type"]
        known_line   = f"Знаю: *{known}*\n" if known else ""
        unknown_line = f"Ещё учу: *{unknown}*\n" if unknown else ""
        current_block = (
            f"📊 *Текущая сессия*\n"
            f"_{TYPE_LABEL.get(ex_type, '')}_\n\n"
            f"{known_line}"
            f"{unknown_line}"
            f"Пройдено: *{studied} / {total}*\n"
            f"Осталось: *{total - studied}*\n"
            f"{streak_line}"
        )
    else:
        current_block = (
            f"📊 *Статистика*\n\n{streak_line}"
            f"Активной тренировки нет. Выбери тему в меню и начни! 🚀\n"
        )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="menu_profile")]])
    return current_block + lifetime_block, kb


def build_menu_weak(user_id: int, back_callback: str = "menu_profile",
                    back_label: str = "← Назад") -> tuple[str, InlineKeyboardMarkup]:
    try:
        rows = db.get_weak_verbs(user_id)
        groups: dict[str, list] = {"verbs": [], "vp": [], "adjprep": [], "prep": []}
        for r in rows:
            ex_type, sep, iid = r["verb_v1"].partition("::")
            if sep and ex_type in groups:          # skip legacy un-namespaced rows
                groups[ex_type].append((iid, r["unknown_count"]))
        lines: list[str] = []
        for ex_type in ("verbs", "vp", "adjprep", "prep"):
            grp = groups[ex_type]
            if grp:
                lines.append(f"\n{TYPE_EMOJI[ex_type]} *{TYPE_LABEL[ex_type]}*")
                lines.extend(_format_weak_item(ex_type, iid, cnt) for iid, cnt in grp[:10])
                if len(grp) > 10:
                    lines.append(f"_…и ещё {len(grp) - 10}_")
        body = "\n".join(lines) if lines else (
            "Пока чисто! ✨\n"
            "Сюда попадут карточки, в которых ты ошибаешься. "
            "Пройди тренировку — и слабые места появятся здесь."
        )
    except Exception:
        logger.exception("Failed to load weak items for user %s", user_id)
        body = "Не удалось загрузить данные."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(back_label, callback_data=back_callback)]])
    return f"📋 *Сложные карточки:*\n{body}", kb


def build_menu_history(user_id: int, back_callback: str = "menu_profile",
                       back_label: str = "← Назад") -> tuple[str, InlineKeyboardMarkup]:
    try:
        rows = db.get_history(user_id)
        if rows:
            lines = []
            for r in rows:
                pct = round(r["known"] / r["total"] * 100) if r["total"] else 0
                lines.append(f"📅 {r['finished_at']} — {r['known']}/{r['total']} ({pct}%)")
            body = "\n".join(lines)
            if len(lines) >= 10:
                body += "\n\n_показаны последние 10 сессий_"
        else:
            body = ("Здесь пока пусто.\n"
                    "Пройди первую тренировку — и тут появятся результаты! 🚀")
    except Exception:
        logger.exception("Failed to load history for user %s", user_id)
        body = "Не удалось загрузить данные."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(back_label, callback_data=back_callback)]])
    return f"📈 *История сессий:*\n\n{body}", kb


def build_menu_help(user_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    if user_id is not None:
        rows.append([InlineKeyboardButton("🔔 Напоминания", callback_data="menu_reminders")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="menu_profile")])
    return HELP_TEXT, InlineKeyboardMarkup(rows)


def _tz_label(tz: int) -> str:
    return f"UTC{tz:+d}" if tz else "UTC"


def build_reminder_settings(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    s  = db.get_reminder_settings(user_id)
    tz = _tz_label(s["tz"])
    if s["enabled"]:
        text = (
            f"🔔 *Напоминания включены*\n\n"
            f"Время: *{s['hour']:02d}:00* ({tz})\n\n"
            f"_Бот напомнит в это время, если будут карточки к повторению._"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔕 Выключить", callback_data="rem_toggle")],
            [
                InlineKeyboardButton("◀",                       callback_data="rem_hour_dec"),
                InlineKeyboardButton(f"🕐 {s['hour']:02d}:00",  callback_data="noop"),
                InlineKeyboardButton("▶",                       callback_data="rem_hour_inc"),
            ],
            [
                InlineKeyboardButton("◀",            callback_data="rem_tz_dec"),
                InlineKeyboardButton(f"🌍 {tz}",     callback_data="noop"),
                InlineKeyboardButton("▶",            callback_data="rem_tz_inc"),
            ],
            [InlineKeyboardButton("← Назад", callback_data="menu_help")],
        ])
    else:
        text = (
            "🔕 *Напоминания выключены*\n\n"
            "_Включи, чтобы бот в удобное время напоминал о карточках к повторению._"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Включить", callback_data="rem_toggle")],
            [InlineKeyboardButton("← Назад",    callback_data="menu_help")],
        ])
    return text, kb


def build_size_selector(exercise_type: str, type_mode: bool = False,
                        user_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    # Mixed is the "quick interleaved sampler" — sized by count, not level.
    if exercise_type == "mixed":
        text = f"{TYPE_EMOJI['mixed']} *{TYPE_LABEL['mixed']}*\n\nСколько карточек?"
        rows = [[
            InlineKeyboardButton("10", callback_data="size:10"),
            InlineKeyboardButton("20", callback_data="size:20"),
            InlineKeyboardButton("30", callback_data="size:30"),
        ], [InlineKeyboardButton("← Назад", callback_data="menu_topics")]]
        return text, InlineKeyboardMarkup(rows)

    # Single types pick by difficulty level, with «освоено / всего» progress.
    cards = CONTENT[exercise_type]
    total = len(cards)
    try:
        known = db.get_known_keys(user_id) if user_id else set()
    except Exception:
        known = set()

    def done_of(items):
        return sum(1 for i in items if card_key(i) in known)

    text = (f"{TYPE_EMOJI.get(exercise_type, '🎯')} *{TYPE_LABEL[exercise_type]}*\n\n"
            f"С чего начнём?\n_цифры — освоено из всего_")
    rows: list[list[InlineKeyboardButton]] = []
    for lvl in (1, 2, 3):
        lvl_items = [i for i in cards if item_level(i) == lvl]
        if lvl_items:
            rows.append([InlineKeyboardButton(
                f"{LEVEL_LABEL[lvl]} · {done_of(lvl_items)}/{len(lvl_items)}",
                callback_data=f"lvl:{lvl}")])
    rows.append([InlineKeyboardButton(
        f"📚 Все · {done_of(cards)}/{total}", callback_data="lvl:all")])

    if user_id:
        try:
            weak_deck = _build_weak_deck(exercise_type, user_id)
            if weak_deck:
                rows.append([InlineKeyboardButton(
                    f"🎯 Только ошибки ({len(weak_deck)})", callback_data="size:weak")])
        except Exception:
            pass

    if exercise_type == "verbs":
        label = "✏️ Выключить ввод V2/V3" if type_mode else "✏️ Включить ввод V2/V3"
        rows.append([InlineKeyboardButton(label, callback_data="toggle_mode")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="menu_topics")])
    return text, InlineKeyboardMarkup(rows)


# ─── Card builders ────────────────────────────────────────────────────────────

def build_verb_card(session: dict, type_mode: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    prog = progress_line(session, item)

    if type_mode:
        text = (
            f"{prog}\n\n"
            f"*{item['v1']}*\n"
            f"_{item['translation']}_\n\n"
            f"✍️ Напиши V2 и V3 через пробел:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💡 Подсказка", callback_data="hint")],
            [InlineKeyboardButton("❓ Не помню",  callback_data="reveal"),
             InlineKeyboardButton("⏸ Пауза",      callback_data="stop_session")],
        ])
    else:
        text = (
            f"{prog}\n\n"
            f"*{item['v1']}*\n"
            f"_{item['translation']}_\n\n"
            f"Вспомни формы — потом загляни в ответ:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁 Показать ответ", callback_data="show")],
            [InlineKeyboardButton("💡 Подсказка",      callback_data="hint")],
            [InlineKeyboardButton("⏸ Пауза",            callback_data="stop_session")],
        ])
    return text, kb


def build_verb_answer(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item      = current_item(session)
    prog      = session.get("_last_progress") or progress_line(session, item)
    forms     = _verb_forms_text(item)
    note_line = f"\n\n📖 _{item['note']}_" if item.get("note") else ""
    text  = (
        f"{prog}\n\n"
        f"{forms}\n"
        f"_{item['translation']}_\n\n"
        f"💬 _{item['example']}_"
        f"{note_line}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Помню",    callback_data="knew"),
            InlineKeyboardButton("❌ Не помню", callback_data="didnt_know"),
        ],
    ])
    return text, kb


def build_prep_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item     = current_item(session)
    sentence = item["sentence"].replace("{?}", "[ ? ]")
    text = (
        f"{progress_line(session, item)}\n\n"
        f"{sentence}\n"
        f"_{item['translation']}_\n\n"
        f"Выбери правильный предлог:"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("in", callback_data="ans:in"),
            InlineKeyboardButton("on", callback_data="ans:on"),
            InlineKeyboardButton("at", callback_data="ans:at"),
        ],
        [InlineKeyboardButton("⏸ Пауза", callback_data="stop_session")],
    ])
    return text, kb


def build_vp_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"*{_vp_display(item)}*\n"
        f"_{item['translation']}_\n\n"
        f"Какой шаблон?"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("+ -ing", callback_data="ans:ing"),
            InlineKeyboardButton("+ to",   callback_data="ans:to"),
        ],
        [InlineKeyboardButton("⏸ Пауза", callback_data="stop_session")],
    ])
    return text, kb


def build_adjprep_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item    = current_item(session)
    rng     = random.Random(item_id(item))
    options = item["options"].copy()
    rng.shuffle(options)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"*{item['adjective']}* + ?\n"
        f"_{item['translation']}_\n\n"
        f"Выбери правильный предлог:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"ans:{opt}") for opt in options],
        [InlineKeyboardButton("⏸ Пауза", callback_data="stop_session")],
    ])
    return text, kb


def build_choice_result(item: dict, chosen: str, correct: bool) -> tuple[str, InlineKeyboardMarkup]:
    kb_next = InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Дальше", callback_data="next")],
    ])
    head = "✅ *Верно!*" if correct else "❌ *Неверно.*"

    if "sentence" in item:
        full      = item["sentence"].replace("{?}", f"*{item['answer']}*")
        wrong_ans = "" if correct else f" Правильный ответ: *{item['answer']}*"
        return f"{head}{wrong_ans}\n\n{full}\n\n📖 _{item['rule']}_", kb_next

    rule_line = f"\n\n📖 _{item['rule']}_" if item.get("rule") else ""

    if "adjective" in item:
        wrong_ans = "" if correct else f" Правильный ответ: *{item['adjective']} {item['preposition']}*"
        return (
            f"{head}{wrong_ans}\n\n"
            f"*{item['adjective']}* + *{item['preposition']}*\n\n"
            f"💬 _{item['example']}_"
            f"{rule_line}",
            kb_next,
        )

    verb_display = _vp_display(item)
    wrong_ans    = "" if correct else f" Правильный ответ: *{verb_display} + {item['pattern']}*"
    return (
        f"{head}{wrong_ans}\n\n"
        f"*{verb_display}* + *{item['pattern']}*\n\n"
        f"💬 _{item['example']}_"
        f"{rule_line}",
        kb_next,
    )


def build_type_result(item: dict, user_input: str | None, correct: bool,
                      hinted: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    forms     = _verb_forms_text(item)
    note_line = f"\n\n📖 _{item['note']}_" if item.get("note") else ""
    kb_next   = InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Дальше", callback_data="next")],
    ])
    if correct:
        head = ("🟡 *Верно, но с подсказкой* — повторим, чтобы запомнить:"
                if hinted else "✅ *Верно!*")
        return (
            f"{head}\n\n"
            f"{forms}\n\n"
            f"💬 _{item['example']}_"
            f"{note_line}",
            kb_next,
        )
    head = ("❌ *Не помню* — вот формы:" if user_input is None
            else f"❌ *Твой ответ:* `{_sanitize_user_text(user_input)}`")
    return (
        f"{head}\n\n"
        f"{forms}\n"
        f"_{item['translation']}_\n\n"
        f"💬 _{item['example']}_"
        f"{note_line}",
        kb_next,
    )


def build_end_review_intro(count: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🏁 *Основная колода пройдена!*\n\n"
        f"Карточек с ошибками: *{count}*.\n"
        f"Закрепим их повторением — или завершим тренировку."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Повторить ошибки", callback_data="start_review")],
        [InlineKeyboardButton("🏁 Завершить",         callback_data="finish_session")],
    ])
    return text, kb


def build_final(session: dict, streak: int) -> tuple[str, InlineKeyboardMarkup]:
    results, known, unknown = _session_outcomes(session)
    total    = len(results)
    pct      = round(known / total * 100) if total else 0
    ex_type  = session["exercise_type"]

    if pct == 100:   grade = "🏆 Идеально! Ты знаешь всё!"
    elif pct >= 80:  grade = "🌟 Отличный результат!"
    elif pct >= 60:  grade = "📈 Хороший прогресс! Продолжай так!"
    elif pct >= 40:  grade = "💪 Не останавливайся, всё получится!"
    else:            grade = "📖 Регулярные повторения — ключ к успеху!"

    streak_line = f"🔥 Серия: *{streak} {_streak_label(streak)}*\n" if streak else ""

    size_label = f"{session['original_total']} {_card_plural_nom(session['original_total'])}"
    subtitle   = f"_{TYPE_LABEL.get(ex_type, '')} · {size_label}_\n\n"

    unknown_keys  = [k for k, ok in results.items() if not ok]
    unknown_block = ""
    if unknown_keys:
        weak_counts: dict = {}
        try:
            _uid = session.get("user_id")
            if _uid:
                weak_counts = db.get_weak_ids(_uid)
        except Exception:
            pass
        unknown_keys.sort(key=lambda k: weak_counts.get(k, 0), reverse=True)
        lines = []
        for key in unknown_keys:
            kind, _, iid = key.partition("::")
            if kind == "verbs" and iid in VERBS_BY_V1:
                v = VERBS_BY_V1[iid]
                if v["v2"] == v["v3"]:
                    lines.append(f"• `{v['v1']}` → `{v['v2']}`")
                else:
                    lines.append(f"• `{v['v1']}` → `{v['v2']}` / `{v['v3']}`")
            elif kind == "vp" and iid in VP_BY_VERB:
                vp = VP_BY_VERB[iid]
                lines.append(f"• `{_vp_display(vp)}` + `{vp['pattern']}`")
            elif kind == "adjprep" and iid in ADJ_BY_ADJ:
                a = ADJ_BY_ADJ[iid]
                lines.append(f"• `{a['adjective']}` + `{a['preposition']}`")
            elif kind == "prep" and iid in PREP_BY_SENT:
                p     = PREP_BY_SENT[iid]
                short = iid.replace("{?}", f"[{p['answer']}]")
                lines.append(f"• _{short}_")
        if lines:
            unknown_block = "\n\n*Повтори:*\n" + "\n".join(lines)

    known_line   = f"Знаю: *{known}* {_card_plural(known)}\n" if known else ""
    unknown_line = f"Ещё учу: *{unknown}* {_card_plural(unknown)}\n" if unknown else ""
    text = (
        f"🎉 *Сессия завершена!*\n"
        f"{subtitle}"
        f"{known_line}"
        f"{unknown_line}"
        f"Результат: *{pct}%*\n"
        f"{streak_line}"
        f"\n{grade}"
        f"{unknown_block}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Ещё раунд", callback_data="repeat_session")],
        [
            InlineKeyboardButton("📋 Сложные", callback_data="final_weak"),
            InlineKeyboardButton("📈 История", callback_data="final_history"),
        ],
        [InlineKeyboardButton("🏠 Другая тема", callback_data="new_session")],
    ])
    return text, kb


# ─── Session mutations ────────────────────────────────────────────────────────

def mark_known(session: dict, item: dict) -> None:
    session["results"][card_key(item)] = True


def mark_unknown(session: dict, item: dict) -> None:
    key = card_key(item)
    session["results"][key] = False
    session.setdefault("ever_wrong", set()).add(key)   # missed at least once
    if session["phase"] != PHASE_MAIN:
        return
    if not any(card_key(v) == key for v, _ in session["review_buffer"]):
        session["review_buffer"].append((item, random.randint(2, 3)))
    if not any(card_key(v) == key for v in session["end_review"]):
        session["end_review"].append(item)


def _session_outcomes(session: dict) -> tuple[dict, int, int]:
    """Per-card effective outcome for stats: a card counts as known only if it
    was never missed this session — an error anywhere (or a used hint) keeps it
    as «ещё учу», so corrected-after-a-slip cards still surface in «Сложные».
    Returns (effective {key: bool}, known, unknown)."""
    ever = session.get("ever_wrong", set())
    effective = {k: (v and k not in ever) for k, v in session["results"].items()}
    known = sum(1 for v in effective.values() if v)
    return effective, known, len(effective) - known


def advance(session: dict) -> None:
    # Clear the hint flag for the card being left so it starts clean on its next appearance.
    cur = session["queue"][session["pos"]] if session["pos"] < len(session["queue"]) else None
    if cur:
        session.get("hint_used", set()).discard(card_key(cur))
    session["pos"] += 1
    new_buf = []
    for item, countdown in session["review_buffer"]:
        countdown -= 1
        if countdown <= 0:
            session["queue"].insert(session["pos"], item)
        else:
            new_buf.append((item, countdown))
    session["review_buffer"] = new_buf


# ─── Rendering helpers ────────────────────────────────────────────────────────

async def safe_edit(bot, chat_id: int, message_id: int,
                    text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=text, parse_mode="Markdown", reply_markup=kb,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def show_card(chat_id: int, session: dict, bot, type_mode: bool = False) -> None:
    item = current_item(session)
    if item is None:
        await show_results(chat_id, session, bot)
        return

    ex_type = item_type(item)          # per-card, so mixed/review decks work
    tm = type_mode and ex_type == "verbs"   # input applies to any verb card
    if ex_type == "verbs":
        text, kb = build_verb_card(session, type_mode=tm)
    elif ex_type == "prep":
        text, kb = build_prep_card(session)
    elif ex_type == "adjprep":
        text, kb = build_adjprep_card(session)
    else:
        text, kb = build_vp_card(session)

    # In type mode the card itself is the prompt — accept a typed answer right away.
    session["awaiting_input"] = tm
    session["_on_result"] = False          # a question is on screen, not a result
    session["_last_progress"] = progress_line(session, item)
    await safe_edit(bot, chat_id, session["message_id"], text, kb)
    session["first_shown"].add(card_key(item))


async def show_results(chat_id: int, session: dict, bot) -> None:
    session["_on_result"] = False          # intro/final screens are not card results
    if session["phase"] == PHASE_MAIN and session["end_review"]:
        deck = session["end_review"].copy()
        random.shuffle(deck)
        session.update({
            "phase": PHASE_END_REVIEW, "queue": deck, "pos": 0,
            "review_buffer": [], "hint_used": set(),
        })
        text, kb = build_end_review_intro(len(deck))
        await safe_edit(bot, chat_id, session["message_id"], text, kb)
        return

    streak  = 0
    user_id = session.get("user_id")
    if user_id:
        effective, known, unknown = _session_outcomes(session)
        streak = db.save_session(user_id, known, unknown, len(effective), effective)

    text, kb = build_final(session, streak)
    await safe_edit(bot, chat_id, session["message_id"], text, kb)


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    is_new   = db.ensure_user(update.effective_user.id)
    try:
        await update.message.delete()
    except Exception:
        pass

    text, kb    = build_type_selector(welcome=is_new, user_id=update.effective_user.id,
                                      resume=_resume_info(context))
    card_msg_id = context.user_data.get("card_message_id")
    if card_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=card_msg_id,
                text=text, parse_mode="Markdown", reply_markup=kb,
            )
            return
        except BadRequest:
            pass

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb,
    )
    context.user_data["card_message_id"] = msg.message_id


async def _render_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       text: str, kb: InlineKeyboardMarkup) -> None:
    """Render a menu screen onto the single card message, deleting the command."""
    chat_id = update.effective_chat.id
    try:
        await update.message.delete()
    except Exception:
        pass
    card_msg_id = context.user_data.get("card_message_id")
    if card_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=card_msg_id,
                text=text, parse_mode="Markdown", reply_markup=kb,
            )
            return
        except BadRequest:
            pass
    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb,
    )
    context.user_data["card_message_id"] = msg.message_id


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_menu_stats(context.user_data.get("session"), update.effective_user.id)
    await _render_menu(update, context, text, kb)


def _format_weak_item(ex_type: str, iid: str, error_count: int) -> str:
    if ex_type == "verbs" and iid in VERBS_BY_V1:
        v = VERBS_BY_V1[iid]
        forms = f"`{v['v2']}`" if v["v2"] == v["v3"] else f"`{v['v2']}` / `{v['v3']}`"
        return f"• `{iid}` → {forms} — ошибок: {error_count}"
    if ex_type == "adjprep" and iid in ADJ_BY_ADJ:
        a = ADJ_BY_ADJ[iid]
        return f"• `{iid}` + *{a['preposition']}* — ошибок: {error_count}"
    if ex_type == "vp" and iid in VP_BY_VERB:
        vp = VP_BY_VERB[iid]
        return f"• `{_vp_display(vp)}` + *{vp['pattern']}* — ошибок: {error_count}"
    short = iid.replace("{?}", "[ ? ]")
    short = short[:50] + "…" if len(short) > 50 else short
    return f"• _{short}_ — ошибок: {error_count}"


async def cmd_weak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_menu_weak(update.effective_user.id)
    await _render_menu(update, context, text, kb)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_menu_history(update.effective_user.id)
    await _render_menu(update, context, text, kb)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = context.user_data.get("type_mode", False)
    context.user_data["type_mode"] = not current
    msg = (
        "✏️ *Режим ввода включён*\n\nДля глаголов: печатай V2 и V3 через пробел."
        if context.user_data["type_mode"] else
        "👆 *Режим кнопок включён*"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Главное меню", callback_data="back_to_types")]])
    await _render_menu(update, context, msg, kb)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_menu_help(update.effective_user.id)
    await _render_menu(update, context, text, kb)


def _admin_id() -> int | None:
    try:
        return int(os.environ["ADMIN_ID"])
    except (KeyError, ValueError):
        return None


async def cmd_resetprogress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only, irreversible: wipe progress/history/streaks for ALL users.
    Requires explicit confirmation: /resetprogress CONFIRM"""
    admin_id = _admin_id()
    if admin_id is None or update.effective_user.id != admin_id:
        return
    arg = (context.args[0].upper() if context.args else "")
    if arg != "CONFIRM":
        await update.message.reply_text(
            "⚠️ *Сброс прогресса у ВСЕХ пользователей* (история, статистика "
            "карточек, серии) — это *необратимо*.\nПользователи и напоминания "
            "сохранятся.\n\nПодтверди: `/resetprogress CONFIRM`",
            parse_mode="Markdown",
        )
        return
    res = db.reset_all_progress()
    await update.message.reply_text(
        f"✅ Прогресс сброшен.\n"
        f"Сессий удалено: *{res['sessions']}*\n"
        f"Статистики карточек: *{res['verb_stats']}*\n"
        f"Серии обнулены у *{res['users']}* пользователей.\n"
        f"_Аккаунты и настройки напоминаний сохранены._",
        parse_mode="Markdown",
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = _admin_id()
    if admin_id is None or update.effective_user.id != admin_id:
        return
    s     = db.get_admin_stats()
    daily = "\n".join(
        f"  {r['finished_at']}: {r['cnt']} сессий" for r in s["daily"]
    ) or "  нет данных"
    await update.message.reply_text(
        f"📊 *Статистика бота*\n\n"
        f"👥 Всего пользователей: *{s['total_users']}*\n"
        f"📅 Активных за 7 дней: *{s['active_7d']}*\n"
        f"📅 Активных за 30 дней: *{s['active_30d']}*\n"
        f"🎯 Всего сессий: *{s['total_sessions']}*\n"
        f"🗓 Сегодня сессий: *{s['today_sessions']}*\n\n"
        f"📈 *По дням (7 дней):*\n{daily}",
        parse_mode="Markdown",
    )


# ─── Callback handler ─────────────────────────────────────────────────────────

async def _launch_session(context, query, chat_id, session, ex_type, last_size, type_mode):
    """Common tail for starting a deck: remember it for «Ещё раунд», bind it to
    the card message, and render the first card."""
    context.user_data["last_type"] = ex_type
    context.user_data["last_size"] = last_size
    msg_id = context.user_data.get("card_message_id") or query.message.message_id
    session["message_id"] = msg_id
    context.user_data["card_message_id"] = msg_id
    context.user_data["session"] = session
    await show_card(chat_id, session, context.bot, type_mode=type_mode)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Acknowledge the callback exactly once. A handler may answer with an alert
    popup; otherwise this fallback clears the button's loading spinner.
    Telegram rejects a second answer to the same query, so it's guarded."""
    query = update.callback_query
    try:
        await _on_button_impl(update, context)
    finally:
        try:
            await query.answer()
        except BadRequest:
            pass


async def _on_button_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query        = update.callback_query
    data         = query.data
    action, arg  = parse_callback(data)     # ("pick"|"size"|"ans", value) or (data, None)
    chat_id      = query.message.chat_id
    user_id      = query.from_user.id
    type_mode    = context.user_data.get("type_mode", False)

    # Heal/drop a session that survived a redeploy with an older shape.
    _raw = context.user_data.get("session")
    if _raw is not None and normalize_session(_raw) is None:
        context.user_data.pop("session", None)

    # ── Pause (keep the session so it can be resumed from the main menu) ──
    if data == "stop_session":
        paused = context.user_data.get("session")
        if paused:
            # The card is hidden behind the menu now — stop treating typed text
            # as an answer or as «Дальше» until the session is resumed.
            paused["awaiting_input"] = False
            paused["_on_result"] = False
        resume = _resume_info(context)
        text, kb = build_type_selector(user_id=user_id, resume=resume)
        if resume:
            text = "_⏸ Тренировка на паузе — продолжишь, когда удобно._\n\n" + text
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "resume_session":
        session = context.user_data.get("session")
        if session is None or _resume_info(context) is None:
            text, kb = build_type_selector(user_id=user_id)
            await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
            return
        session["message_id"] = query.message.message_id
        context.user_data["card_message_id"] = query.message.message_id
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
        return

    # ── Menu screens ──
    if data == "menu_topics":
        text, kb = build_menu_topics()
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "menu_profile":
        text, kb = build_menu_profile(user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "menu_stats":
        session = context.user_data.get("session")
        text, kb = build_menu_stats(session, user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "menu_weak":
        text, kb = build_menu_weak(user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "menu_history":
        text, kb = build_menu_history(user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "menu_help":
        text, kb = build_menu_help(user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "menu_reminders":
        db.ensure_user(user_id)
        text, kb = build_reminder_settings(user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data in ("rem_toggle", "rem_hour_inc", "rem_hour_dec", "rem_tz_inc", "rem_tz_dec"):
        db.ensure_user(user_id)
        s = db.get_reminder_settings(user_id)
        if   data == "rem_toggle":   db.set_reminders(user_id, not s["enabled"])
        elif data == "rem_hour_inc": db.set_reminder_hour(user_id, s["hour"] + 1)
        elif data == "rem_hour_dec": db.set_reminder_hour(user_id, s["hour"] - 1)
        elif data == "rem_tz_inc":   db.set_tz_offset(user_id, s["tz"] + 1)
        elif data == "rem_tz_dec":   db.set_tz_offset(user_id, s["tz"] - 1)
        text, kb = build_reminder_settings(user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "noop":
        return                       # display-only stepper label

    if data == "reminders_off":
        db.set_reminders(user_id, False)
        await query.answer("🔕 Напоминания отключены. Включить — в разделе «Помощь».",
                           show_alert=True)
        return

    # ── Navigation ──
    if data == "back_to_types":
        text, kb = build_type_selector(user_id=user_id, resume=_resume_info(context))
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "toggle_mode":
        context.user_data["type_mode"] = not type_mode
        ex_type = context.user_data.get("pending_type", "verbs")
        text, kb = build_size_selector(ex_type, type_mode=context.user_data["type_mode"],
                                       user_id=user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if action == "pick" and (arg in CONTENT or arg == "mixed"):
        context.user_data["pending_type"] = arg
        text, kb = build_size_selector(arg, type_mode=type_mode, user_id=user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if action == "lvl":                       # difficulty tier for a single type
        ex_type = context.user_data.get("pending_type", "verbs")
        if arg == "all":
            session, last_size = new_session(ex_type, user_id=user_id), "all"
        elif arg in ("1", "2", "3"):
            deck = _level_deck(ex_type, int(arg))
            if not deck:
                await query.answer("На этом уровне карточек пока нет.", show_alert=True)
                return
            session, last_size = new_session(ex_type, deck=deck, user_id=user_id), f"lvl:{arg}"
        else:
            return                            # ignore any crafted lvl:<garbage>
        await _launch_session(context, query, chat_id, session, ex_type, last_size, type_mode)
        return

    if action == "size":                      # mixed counts, or «только ошибки»
        ex_type = context.user_data.get("pending_type", "verbs")
        if arg == "weak":
            weak_deck = _build_weak_deck(ex_type, user_id)
            if not weak_deck:
                await query.answer("Ошибок пока нет!", show_alert=True)
                return
            session, last_size = new_session(ex_type, user_id=user_id, deck=weak_deck), "weak"
        else:
            size_map = {"10": 10, "20": 20, "30": 30, "all": None}
            if arg not in size_map:
                return                        # ignore any crafted size:<garbage>
            session, last_size = new_session(ex_type, size=size_map[arg], user_id=user_id), arg
        await _launch_session(context, query, chat_id, session, ex_type, last_size, type_mode)
        return

    if data == "start_due":
        deck = _build_daily_deck(user_id)
        if not deck:
            await query.answer("На сегодня всё пройдено — загляни позже 👍", show_alert=True)
            return
        session = new_session("review", deck=deck, user_id=user_id)
        context.user_data["last_type"] = "review"
        context.user_data["last_size"] = "review"
        msg_id = query.message.message_id          # render on the tapped msg (menu or reminder)
        session["message_id"] = msg_id
        context.user_data["card_message_id"] = msg_id
        context.user_data["session"] = session
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
        return

    if data == "new_session":
        context.user_data.pop("session", None)        # «Другая тема» — clear finished session
        text, kb = build_type_selector(user_id=user_id)
        msg_id   = context.user_data.get("card_message_id") or query.message.message_id
        context.user_data["card_message_id"] = msg_id
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, parse_mode="Markdown", reply_markup=kb,
            )
        except BadRequest:
            pass
        return

    if data == "repeat_session":
        ex_type   = context.user_data.get("last_type", "verbs")
        last_size = context.user_data.get("last_size", "all")
        if last_size == "weak":
            weak_deck = _build_weak_deck(ex_type, user_id)
            if not weak_deck:
                await query.answer("Ошибок больше нет — отличная работа! 🎉", show_alert=True)
                return
            session = new_session(ex_type, user_id=user_id, deck=weak_deck)
        elif last_size == "review":
            deck = _build_daily_deck(user_id)
            if not deck:
                await query.answer("На сегодня всё пройдено — загляни позже 👍", show_alert=True)
                return
            session = new_session("review", user_id=user_id, deck=deck)
        elif last_size.startswith("lvl:"):
            deck = _level_deck(ex_type, int(last_size[4:]))
            if not deck:
                await query.answer("На этом уровне карточек пока нет.", show_alert=True)
                return
            session = new_session(ex_type, deck=deck, user_id=user_id)
        else:
            size_map = {"10": 10, "20": 20, "30": 30, "all": None}
            session  = new_session(ex_type, size=size_map.get(last_size), user_id=user_id)
        msg_id = context.user_data.get("card_message_id") or query.message.message_id
        session["message_id"] = msg_id
        context.user_data["card_message_id"] = msg_id
        context.user_data["session"] = session
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
        return

    # ── Final screen inline stats ──
    if data == "final_weak":
        text, kb = build_menu_weak(user_id, back_callback="back_to_final",
                                   back_label="← Назад к результатам")
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "final_history":
        text, kb = build_menu_history(user_id, back_callback="back_to_final",
                                      back_label="← Назад к результатам")
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "back_to_final":
        session = context.user_data.get("session")
        if session:
            streak   = db.get_streak(user_id)
            text, kb = build_final(session, streak)
            await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    # ── Needs an active session ──
    session = context.user_data.get("session")
    if not session:
        context.user_data["card_message_id"] = query.message.message_id
        text, kb = build_type_selector(user_id=user_id)
        text = "_Активная сессия не найдена — выбери тему заново._\n\n" + text
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    # Ignore callbacks from old card messages (user scrolled up and tapped a stale button).
    # Menu-navigation callbacks that target query.message.message_id directly are exempt.
    _session_mutating = frozenset({
        "ans", "knew", "didnt_know", "reveal", "next",
        "hint", "show", "finish_session", "start_review",
    })
    if (action in _session_mutating or data in _session_mutating) and \
            query.message.message_id != session.get("message_id"):
        await query.answer()
        return

    if data == "start_review":
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
        return

    if data == "finish_session":
        session["end_review"] = []          # skip optional review, finalize & save
        await show_results(chat_id, session, context.bot)
        return

    item = current_item(session)
    if item is None:
        await show_results(chat_id, session, context.bot)
        return

    # ── Multiple-choice answers ──
    if action == "ans":
        chosen         = arg
        correct_answer = item.get("answer") or item.get("pattern") or item.get("preposition")
        correct        = chosen == correct_answer
        text, kb       = build_choice_result(item, chosen, correct)
        if correct:
            mark_known(session, item)
        else:
            mark_unknown(session, item)
        session["_on_result"] = True
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)
        return

    # ── Verb callbacks ──
    if data == "show":
        text, kb = build_verb_answer(session)
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "hint":
        if item_type(item) != "verbs":
            return
        tm   = type_mode             # item is a verb here; input applies anywhere
        # A peeked hint means the user couldn't recall unaided — mark ever_wrong
        # regardless of mode so self-reported ✅ after a hint still counts as a slip.
        session.setdefault("ever_wrong", set()).add(card_key(item))
        if tm:
            session.setdefault("hint_used", set()).add(card_key(item))
        prog = session.get("_last_progress") or progress_line(session, item)
        v2   = item["v2"].split("/")[0]
        v3   = item["v3"].split("/")[0]
        if set(_norm_forms(item["v2"])) == set(_norm_forms(item["v3"])):
            hint_line = f"💡 Форма на *{v2[0].lower()}…* ({len(v2)} букв)"
        else:
            hint_line = (f"💡 V2 на *{v2[0].lower()}…* ({len(v2)}) · "
                         f"V3 на *{v3[0].lower()}…* ({len(v3)})")
        base = (f"{prog}\n\n*{item['v1']}*\n_{item['translation']}_\n\n{hint_line}")
        if tm:
            text = f"{base}\n\n✍️ Напиши V2 и V3 через пробел:"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❓ Не помню", callback_data="reveal"),
                 InlineKeyboardButton("⏸ Пауза",     callback_data="stop_session")],
            ])
        else:
            text = f"{base}\n\nВспомни формы — потом загляни в ответ:"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👁 Показать ответ", callback_data="show")],
                [InlineKeyboardButton("⏸ Пауза",            callback_data="stop_session")],
            ])
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "knew":
        mark_known(session, item)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "didnt_know":
        mark_unknown(session, item)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "reveal":                     # «Не помню» in type mode → show forms
        session["awaiting_input"] = False
        mark_unknown(session, item)
        session["_on_result"] = True
        text, kb = build_type_result(item, None, correct=False)
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "next":
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = context.user_data.get("session")
    if session is not None and normalize_session(session) is None:
        context.user_data.pop("session", None)
        session = None
    if not session:
        return

    chat_id   = update.effective_chat.id
    type_mode = context.user_data.get("type_mode", False)

    # In type mode, any text on a card RESULT screen acts as «Дальше». The
    # _on_result flag distinguishes results from question screens (a typed
    # «in» on a prep card must not skip it) and from the end-review intro.
    if not session.get("awaiting_input"):
        if type_mode and session.get("_on_result") and current_item(session) is not None:
            try:
                await update.message.delete()
            except Exception:
                pass
            advance(session)
            await show_card(chat_id, session, context.bot, type_mode=True)
        return

    item = current_item(session)
    if not item:
        return

    session["awaiting_input"] = False
    try:
        await update.message.delete()
    except Exception:
        pass
    raw   = update.message.text.strip()
    parts = _norm_forms(raw)

    expected_v2 = _norm_forms(item["v2"])
    expected_v3 = _norm_forms(item["v3"])
    if set(expected_v2) == set(expected_v3):
        # V2 == V3 (e.g. cut/cut/cut): one word is enough, but extra words must
        # still be the right form — «sat sitten» is wrong, not correct.
        correct = bool(parts) and all(p in expected_v2 for p in parts)
    else:
        # All tokens before the last must be valid V2 forms; the last must be V3.
        # This accepts "went gone" (normal) and "was/were been" (multi-variant V2)
        # while rejecting "went garbage gone" (extra unrecognised tokens).
        correct = (
            len(parts) >= 2
            and parts[-1] in expected_v3
            and all(p in expected_v2 for p in parts[:-1])
        )

    hinted = card_key(item) in session.get("hint_used", set())
    text, kb = build_type_result(item, raw, correct, hinted=hinted)
    if correct and not hinted:
        mark_known(session, item)
    else:
        mark_unknown(session, item)        # wrong, or right-but-with-a-hint
    session["_on_result"] = True
    try:
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)
    except BadRequest:
        pass


# ─── Entry point ──────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Clear the Telegram slash-command menu — the bot is fully button-driven.
    Command handlers still work as silent fallbacks (e.g. /start), they just
    aren't advertised in the UI."""
    await app.bot.set_my_commands([])


async def send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs hourly: DM users whose personal reminder time is this UTC hour and
    who have due cards but haven't studied today."""
    utc_hour = dt.datetime.now(dt.timezone.utc).hour
    try:
        targets = db.get_reminder_targets(utc_hour=utc_hour)
    except Exception:
        logger.exception("Failed to fetch reminder targets")
        return
    for uid in targets:
        try:
            reviews, new = _daily_counts(uid)
            cnt = reviews + new
            if not cnt:
                continue
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔔 Тренировка дня ({cnt})", callback_data="start_due")],
                [InlineKeyboardButton("🔕 Отключить", callback_data="reminders_off")],
            ])
            await context.bot.send_message(
                uid,
                f"🔔 *Тренировка дня готова!*\n\n"
                f"*{cnt}* {_card_plural_nom(cnt)}: "
                f"{f'{reviews} на повтор + {new} к изучению' if reviews and new else f'{reviews} на повтор' if reviews else f'{new} к изучению'}.\n"
                f"Несколько минут — и слова закрепятся надолго 💪",
                parse_mode="Markdown", reply_markup=kb,
            )
        except Exception:
            logger.warning("Reminder to %s failed (blocked/deleted?)", uid)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any unhandled exception and, for message updates, notify the user."""
    logger.exception("Unhandled error while processing update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Что-то пошло не так. Попробуй ещё раз или нажми /start."
            )
        except Exception:
            pass


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    db.init_db()

    # Persist user state (active session, settings) across restarts/redeploys.
    # Defaults next to the DB so a single mounted volume covers both.
    state_path = os.environ.get(
        "STATE_PATH",
        os.path.join(os.path.dirname(db.DB_PATH) or ".", "bot_state.pkl"),
    )
    persistence = PicklePersistence(filepath=state_path)

    app = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("weak",    cmd_weak))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("mode",    cmd_mode))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("admin",   cmd_admin))
    app.add_handler(CommandHandler("resetprogress", cmd_resetprogress))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    # Spaced-repetition reminders: check every hour and ping users whose
    # personal time (reminder_hour - tz_offset) matches the current UTC hour.
    if app.job_queue is not None:
        now   = dt.datetime.now(dt.timezone.utc)
        first = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        app.job_queue.run_repeating(send_reminders, interval=3600, first=first)
        logger.info("Hourly reminder check scheduled (next at %s)", first.isoformat())
    else:
        logger.warning("JobQueue unavailable — reminders off "
                       "(needs python-telegram-bot[job-queue])")

    logger.info("Bot is running… (state: %s)", state_path)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
