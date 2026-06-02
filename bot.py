import os
import random
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, PicklePersistence,
)
from telegram.error import BadRequest

from verbs import VERBS
from prepositions import PREPOSITIONS
from verb_patterns import VERB_PATTERNS
from adj_preps import ADJ_PREPS
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
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

TYPE_EMOJI = {"verbs": "🔤", "prep": "📍", "vp": "➕", "adjprep": "🔗"}
TYPE_LABEL = {
    "verbs":   "Неправильные глаголы",
    "prep":    "Предлоги in / on / at",
    "vp":      "Глаголы + to / -ing",
    "adjprep": "Прилагательные + предлог",
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
    "*В меню выбора колоды:*\n"
    "🎯 Только ошибки — тренировка только сложных карточек\n"
    "✏️ Режим ввода — печатать V2 и V3 вместо кнопок\n\n"
    "*На карточке глагола:*\n"
    "💡 Подсказка — первая буква и длина V2\n\n"
    "*Команды:*\n"
    "/start — главное меню\n"
    "/stats — статистика\n"
    "/weak — сложные карточки\n"
    "/history — история сессий\n"
    "/mode — переключить режим ввода (для глаголов)\n"
    "/help — эта справка"
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def item_id(item: dict) -> str:
    return item.get("v1") or item.get("verb") or item.get("adjective") or item.get("sentence", "?")


def _streak_label(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:                  return "день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return "дня"
    return "дней"


def _card_plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:                  return "карточку"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return "карточки"
    return "карточек"


def _progress_bar(session: dict) -> str:
    if session.get("phase") == "end_review":
        return ""
    results = session["results"]
    total   = session["original_total"]
    if not total:
        return ""
    known   = sum(1 for v in results.values() if v)
    unknown = sum(1 for v in results.values() if not v)
    n = 10
    k = round(known   * n / total)
    u = min(round(unknown * n / total), n - k)
    e = n - k - u
    return "🟩" * k + "🟥" * u + "⬜️" * e


def _verb_forms_text(item: dict) -> str:
    v1, v2, v3 = item["v1"], item["v2"], item["v3"]
    if v1 == v2 == v3:
        return f"✅ Все формы одинаковые: `{v1}`"
    if v2 == v3:
        return f"*V2 = V3:* `{v2}`"
    return f"*V2:* `{v2}`\n*V3:* `{v3}`"


def _vp_display(item: dict) -> str:
    return item["verb"].split("  ")[0].strip()


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


# ─── Session ──────────────────────────────────────────────────────────────────

def new_session(exercise_type: str, size: int | None = None,
                user_id: int | None = None, deck: list | None = None) -> dict:
    if deck is not None:
        d = deck.copy()
    else:
        d = CONTENT[exercise_type].copy()
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
        "phase":          "main",
        "message_id":     None,
        "awaiting_input": False,
        "user_id":        user_id,
    }


def current_item(session: dict) -> dict | None:
    q, p = session["queue"], session["pos"]
    return q[p] if p < len(q) else None


# ─── Progress line ────────────────────────────────────────────────────────────

def progress_line(session: dict, item: dict) -> str:
    emoji = TYPE_EMOJI.get(session.get("exercise_type", "verbs"), "📚")
    bar   = _progress_bar(session)

    if session["phase"] == "end_review":
        pos   = session["pos"] + 1
        total = len(session["queue"])
        return f"🔄 *Повторение {pos} / {total}*"

    iid    = item_id(item)
    is_new = iid not in session["first_shown"]
    done   = len(session["first_shown"])
    total  = session["original_total"]

    counter = f"{done + 1} / {total}" if is_new else f"Повтор · {done} / {total}"
    prefix  = emoji if is_new else "🔄"
    return f"{prefix} *{counter}*\n{bar}"


# ─── Selectors ────────────────────────────────────────────────────────────────

def build_type_selector(welcome: bool = False) -> tuple[str, InlineKeyboardMarkup]:
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
    else:
        text = "📚 *Что хочешь потренировать?*"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤 Неправильные глаголы",       callback_data="type_verbs")],
        [InlineKeyboardButton("📍 Предлоги in / on / at",      callback_data="type_prep")],
        [InlineKeyboardButton("➕ Глаголы + to / -ing",        callback_data="type_vp")],
        [InlineKeyboardButton("🔗 Прилагательные + предлог",   callback_data="type_adjprep")],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
            InlineKeyboardButton("📋 Сложные",    callback_data="menu_weak"),
        ],
        [
            InlineKeyboardButton("📈 История",    callback_data="menu_history"),
            InlineKeyboardButton("❓ Помощь",     callback_data="menu_help"),
        ],
    ])
    return text, kb


def build_menu_stats(session: dict | None, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    streak      = db.get_streak(user_id)
    streak_line = f"🔥 Серия: *{streak} {_streak_label(streak)}* подряд\n" if streak else ""
    try:
        lt = db.get_lifetime_stats(user_id)
        lifetime_block = (
            f"\n🏆 *За всё время*\n"
            f"📚 Карточек пройдено: *{lt['total_cards']}*\n"
            f"🎓 Освоено: *{lt['mastered']}*\n"
            f"🎯 Изучается: *{lt['learning']}*\n"
            f"🗓 Сессий: *{lt['sessions']}*"
        ) if lt["sessions"] > 0 else "\nСессий пока не завершено."
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
        current_block = (
            f"📊 *Текущая сессия*\n"
            f"_{TYPE_LABEL.get(ex_type, '')}_\n\n"
            f"✅ Знаю: *{known}*\n"
            f"❌ Учу: *{unknown}*\n"
            f"📚 Пройдено: *{studied} / {total}*\n"
            f"⏳ Осталось: *{total - studied}*\n"
            f"{streak_line}"
        )
    else:
        current_block = f"📊 *Статистика*\n\n{streak_line}Нет активной сессии.\n"

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Главное меню", callback_data="back_to_types")]])
    return current_block + lifetime_block, kb


def build_menu_weak(user_id: int, back_callback: str = "back_to_types",
                    back_label: str = "← Главное меню") -> tuple[str, InlineKeyboardMarkup]:
    try:
        rows = db.get_weak_verbs(user_id)
        groups: dict[str, list] = {"verbs": [], "vp": [], "adjprep": [], "prep": []}
        for r in rows:
            ex_type, sep, iid = r["verb_v1"].partition("::")
            if sep and ex_type in groups:          # skip legacy un-namespaced rows
                groups[ex_type].append((iid, r["unknown_count"]))
        lines: list[str] = []
        for ex_type in ("verbs", "vp", "adjprep", "prep"):
            if groups[ex_type]:
                lines.append(f"\n{TYPE_EMOJI[ex_type]} *{TYPE_LABEL[ex_type]}*")
                lines.extend(_format_weak_item(ex_type, iid, cnt)
                             for iid, cnt in groups[ex_type][:10])
        body = "\n".join(lines) if lines else \
            "Пока нет данных. Пройди хотя бы одну сессию до конца! 📚"
    except Exception:
        logger.exception("Failed to load weak items for user %s", user_id)
        body = "Не удалось загрузить данные."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(back_label, callback_data=back_callback)]])
    return f"📋 *Сложные карточки:*\n{body}", kb


def build_menu_history(user_id: int, back_callback: str = "back_to_types",
                       back_label: str = "← Главное меню") -> tuple[str, InlineKeyboardMarkup]:
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
            body = "История пуста."
    except Exception:
        logger.exception("Failed to load history for user %s", user_id)
        body = "Не удалось загрузить данные."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(back_label, callback_data=back_callback)]])
    return f"📈 *История сессий:*\n\n{body}", kb


def build_menu_help() -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Главное меню", callback_data="back_to_types")]])
    return HELP_TEXT, kb


def build_size_selector(exercise_type: str, type_mode: bool = False,
                        user_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    total = len(CONTENT[exercise_type])
    text  = f"🎯 *{TYPE_LABEL[exercise_type]}*\n\nСколько карточек?"
    row: list[InlineKeyboardButton] = []
    if total > 10:
        row.append(InlineKeyboardButton("10", callback_data="size_10"))
    if total >= 20:
        row.append(InlineKeyboardButton("20", callback_data="size_20"))
    row.append(InlineKeyboardButton(f"Все {total}", callback_data="size_all"))
    rows = [row]

    # "Only errors" button — shown when user has weak items for this type
    if user_id:
        try:
            weak_deck = _build_weak_deck(exercise_type, user_id)
            if weak_deck:
                wc = len(weak_deck)
                rows.append([InlineKeyboardButton(
                    f"🎯 Только ошибки ({wc})", callback_data="size_weak"
                )])
        except Exception:
            pass

    if exercise_type == "verbs":
        label = "✏️ Режим ввода: вкл ✓" if type_mode else "✏️ Режим ввода: выкл"
        rows.append([InlineKeyboardButton(label, callback_data="toggle_mode")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="back_to_types")])
    return text, InlineKeyboardMarkup(rows)


# ─── Card builders ────────────────────────────────────────────────────────────

def build_verb_card(session: dict, type_mode: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    prog = progress_line(session, item)

    if type_mode:
        text = (
            f"{prog}\n\n"
            f"🔤 *{item['v1']}*\n"
            f"🇷🇺 _{item['translation']}_\n\n"
            f"Нажми *Написать*, чтобы ввести V2 и V3:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Написать",  callback_data="type_answer")],
            [InlineKeyboardButton("💡 Подсказка", callback_data="hint"),
             InlineKeyboardButton("⏹ Стоп",      callback_data="stop_session")],
        ])
    else:
        text = (
            f"{prog}\n\n"
            f"🔤 *{item['v1']}*\n"
            f"🇷🇺 _{item['translation']}_\n\n"
            f"Вспомни V2 и V3, затем проверь:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁 Показать ответ", callback_data="show")],
            [InlineKeyboardButton("💡 Подсказка",      callback_data="hint"),
             InlineKeyboardButton("⏹ Стоп",            callback_data="stop_session")],
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
        f"🇷🇺 _{item['translation']}_\n\n"
        f"💬 _{item['example']}_"
        f"{note_line}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Знал(а)",    callback_data="knew"),
            InlineKeyboardButton("❌ Не знал(а)", callback_data="didnt_know"),
        ],
        [InlineKeyboardButton("⏹ Стоп", callback_data="stop_session")],
    ])
    return text, kb


def build_prep_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item     = current_item(session)
    sentence = item["sentence"].replace("{?}", "[ ? ]")
    text = (
        f"{progress_line(session, item)}\n\n"
        f"{sentence}\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"Выбери правильный предлог:"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("in", callback_data="ans_in"),
            InlineKeyboardButton("on", callback_data="ans_on"),
            InlineKeyboardButton("at", callback_data="ans_at"),
        ],
        [InlineKeyboardButton("⏹ Стоп", callback_data="stop_session")],
    ])
    return text, kb


def build_vp_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"🔤 *{_vp_display(item)}*\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"Какой шаблон?"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("+ -ing", callback_data="ans_ing"),
            InlineKeyboardButton("+ to",   callback_data="ans_to"),
        ],
        [InlineKeyboardButton("⏹ Стоп", callback_data="stop_session")],
    ])
    return text, kb


def build_adjprep_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item    = current_item(session)
    rng     = random.Random(item_id(item))
    options = item["options"].copy()
    rng.shuffle(options)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"🔤 *{item['adjective']}* + ?\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"Выбери правильный предлог:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"ans_{opt}") for opt in options],
        [InlineKeyboardButton("⏹ Стоп", callback_data="stop_session")],
    ])
    return text, kb


def build_choice_result(item: dict, chosen: str, correct: bool) -> tuple[str, InlineKeyboardMarkup]:
    kb_next = InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Дальше", callback_data="next")],
        [InlineKeyboardButton("⏹ Стоп",   callback_data="stop_session")],
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


def build_type_prompt(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    prog = session.get("_last_progress") or progress_line(session, item)
    text = (
        f"{prog}\n\n"
        f"✍️ *{item['v1']}* — _{item['translation']}_\n\n"
        f"Напиши V2 и V3 через пробел:"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_type")]])
    return text, kb


def build_type_result(item: dict, user_input: str, correct: bool) -> tuple[str, InlineKeyboardMarkup]:
    forms     = _verb_forms_text(item)
    note_line = f"\n\n📖 _{item['note']}_" if item.get("note") else ""
    kb_next   = InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Дальше", callback_data="next")],
        [InlineKeyboardButton("⏹ Стоп",   callback_data="stop_session")],
    ])
    if correct:
        return (
            f"✅ *Верно!*\n\n"
            f"{forms}\n\n"
            f"💬 _{item['example']}_"
            f"{note_line}",
            kb_next,
        )
    return (
        f"❌ *Ты написал(а):* `{user_input}`\n\n"
        f"{forms}\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"💬 _{item['example']}_"
        f"{note_line}",
        kb_next,
    )


def build_end_review_intro(count: int) -> tuple[str, InlineKeyboardMarkup]:
    if count == 1:
        which, verb = "которая", "вызвала"
    else:
        which, verb = "которые", "вызвали"
    text = (
        f"🏁 *Основная колода пройдена!*\n\n"
        f"Повторим *{count}* {_card_plural(count)}, {which} {verb} затруднение.\n\n"
        f"Готов(а)?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Начать повторение", callback_data="start_review")],
        [InlineKeyboardButton("⏹ Стоп",              callback_data="stop_session")],
    ])
    return text, kb


def build_final(session: dict, streak: int) -> tuple[str, InlineKeyboardMarkup]:
    results  = session["results"]
    known    = sum(1 for v in results.values() if v)
    unknown  = sum(1 for v in results.values() if not v)
    total    = len(results)
    pct      = round(known / total * 100) if total else 0
    ex_type  = session["exercise_type"]

    if pct == 100:   grade = "🏆 Идеально! Ты знаешь всё!"
    elif pct >= 80:  grade = "🌟 Отличный результат!"
    elif pct >= 60:  grade = "📈 Хороший прогресс! Продолжай так!"
    elif pct >= 40:  grade = "💪 Не останавливайся, всё получится!"
    else:            grade = "📖 Регулярные повторения — ключ к успеху!"

    streak_line = f"🔥 Серия: *{streak} {_streak_label(streak)}* подряд\n" if streak else ""

    size_label = f"{session['original_total']} {_card_plural(session['original_total'])}"
    subtitle   = f"_{TYPE_LABEL.get(ex_type, '')} · {size_label}_\n\n"

    unknown_ids   = [iid for iid, ok in results.items() if not ok]
    unknown_block = ""
    if unknown_ids:
        weak_counts: dict = {}
        try:
            _uid = session.get("user_id")
            if _uid:
                weak_counts = db.get_weak_ids(_uid)
        except Exception:
            pass
        unknown_ids.sort(key=lambda x: weak_counts.get(_stat_key(ex_type, x), 0), reverse=True)
        lines = []
        for iid in unknown_ids:
            if ex_type == "verbs" and iid in VERBS_BY_V1:
                v = VERBS_BY_V1[iid]
                if v["v2"] == v["v3"]:
                    lines.append(f"• `{v['v1']}` → `{v['v2']}`")
                else:
                    lines.append(f"• `{v['v1']}` → `{v['v2']}` / `{v['v3']}`")
            elif ex_type == "vp" and iid in VP_BY_VERB:
                vp = VP_BY_VERB[iid]
                lines.append(f"• `{_vp_display(vp)}` + `{vp['pattern']}`")
            elif ex_type == "adjprep" and iid in ADJ_BY_ADJ:
                a = ADJ_BY_ADJ[iid]
                lines.append(f"• `{a['adjective']}` + `{a['preposition']}`")
            elif ex_type == "prep" and iid in PREP_BY_SENT:
                p     = PREP_BY_SENT[iid]
                short = iid.replace("{?}", f"[{p['answer']}]")
                lines.append(f"• _{short}_")
        if lines:
            unknown_block = "\n\n📋 *Повтори:*\n" + "\n".join(lines)

    text = (
        f"🎉 *Сессия завершена!*\n"
        f"{subtitle}"
        f"✅ Знаю: *{known}* {_card_plural(known)}\n"
        f"❌ Учу: *{unknown}* {_card_plural(unknown)}\n"
        f"📊 Результат: *{pct}%*\n"
        f"{streak_line}"
        f"\n{grade}"
        f"{unknown_block}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Ещё раз эту же",  callback_data="repeat_session"),
            InlineKeyboardButton("🏠 Другая тема",      callback_data="new_session"),
        ],
        [
            InlineKeyboardButton("📋 Сложные карточки", callback_data="final_weak"),
            InlineKeyboardButton("📈 История",           callback_data="final_history"),
        ],
    ])
    return text, kb


# ─── Session mutations ────────────────────────────────────────────────────────

def mark_known(session: dict, item: dict) -> None:
    session["results"][item_id(item)] = True


def mark_unknown(session: dict, item: dict) -> None:
    iid = item_id(item)
    session["results"][iid] = False
    if session["phase"] != "main":
        return
    if not any(item_id(v) == iid for v, _ in session["review_buffer"]):
        session["review_buffer"].append((item, random.randint(2, 3)))
    if not any(item_id(v) == iid for v in session["end_review"]):
        session["end_review"].append(item)


def advance(session: dict) -> None:
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
    if not item:
        await show_results(chat_id, session, bot)
        return

    ex_type = session["exercise_type"]
    if ex_type == "verbs":
        text, kb = build_verb_card(session, type_mode=type_mode)
    elif ex_type == "prep":
        text, kb = build_prep_card(session)
    elif ex_type == "adjprep":
        text, kb = build_adjprep_card(session)
    else:
        text, kb = build_vp_card(session)

    session["_last_progress"] = progress_line(session, item)
    await safe_edit(bot, chat_id, session["message_id"], text, kb)
    session["first_shown"].add(item_id(item))


async def show_results(chat_id: int, session: dict, bot) -> None:
    if session["phase"] == "main" and session["end_review"]:
        deck = session["end_review"].copy()
        random.shuffle(deck)
        session.update({
            "phase": "end_review", "queue": deck, "pos": 0, "review_buffer": [],
        })
        text, kb = build_end_review_intro(len(deck))
        await safe_edit(bot, chat_id, session["message_id"], text, kb)
        return

    streak  = 0
    user_id = session.get("user_id")
    if user_id:
        results = session["results"]
        known   = sum(1 for v in results.values() if v)
        unknown = sum(1 for v in results.values() if not v)
        streak  = db.save_session(user_id, known, unknown, len(results),
                                  results, session["exercise_type"])

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

    text, kb    = build_type_selector(welcome=is_new)
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
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
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

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    await query.answer()
    data      = query.data
    chat_id   = query.message.chat_id
    user_id   = query.from_user.id
    type_mode = context.user_data.get("type_mode", False)

    # ── Stop ──
    if data == "stop_session":
        session = context.user_data.pop("session", None)
        text, kb = build_type_selector()
        if session and session["results"]:
            done  = len(session["results"])
            known = sum(1 for v in session["results"].values() if v)
            note  = f"_Сессия прервана — {known} из {done} {_card_plural(done)} пройдено_\n\n"
            text  = note + text
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    # ── Menu screens ──
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
        text, kb = build_menu_help()
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    # ── Navigation ──
    if data == "back_to_types":
        text, kb = build_type_selector()
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "toggle_mode":
        context.user_data["type_mode"] = not type_mode
        ex_type = context.user_data.get("pending_type", "verbs")
        text, kb = build_size_selector(ex_type, type_mode=context.user_data["type_mode"],
                                       user_id=user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data.startswith("type_"):
        ex_type = data[5:]
        context.user_data["pending_type"] = ex_type
        text, kb = build_size_selector(ex_type, type_mode=type_mode, user_id=user_id)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data in ("size_10", "size_20", "size_all", "size_weak"):
        ex_type = context.user_data.get("pending_type", "verbs")

        if data == "size_weak":
            weak_deck = _build_weak_deck(ex_type, user_id)
            if not weak_deck:
                await query.answer("Ошибок пока нет!", show_alert=True)
                return
            session = new_session(ex_type, user_id=user_id, deck=weak_deck)
            context.user_data["last_size"] = "weak"
        else:
            size_map = {"size_10": 10, "size_20": 20, "size_all": None}
            session  = new_session(ex_type, size=size_map[data], user_id=user_id)
            context.user_data["last_size"] = data[5:]  # "10", "20", "all"

        context.user_data["last_type"] = ex_type
        msg_id  = context.user_data.get("card_message_id") or query.message.message_id
        session["message_id"] = msg_id
        context.user_data["card_message_id"] = msg_id
        context.user_data["session"] = session
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
        return

    if data == "new_session":
        text, kb = build_type_selector()
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
        else:
            size_map = {"10": 10, "20": 20, "all": None}
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
        text, kb = build_type_selector()
        text = "_Активная сессия не найдена — выбери тему заново._\n\n" + text
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data == "start_review":
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
        return

    item = current_item(session)
    if not item:
        await show_results(chat_id, session, context.bot)
        return

    # ── Multiple-choice answers ──
    if data.startswith("ans_"):
        chosen         = data[4:]
        correct_answer = item.get("answer") or item.get("pattern") or item.get("preposition")
        correct        = chosen == correct_answer
        text, kb       = build_choice_result(item, chosen, correct)
        if correct:
            mark_known(session, item)
        else:
            mark_unknown(session, item)
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)
        return

    # ── Verb callbacks ──
    if data == "show":
        text, kb = build_verb_answer(session)
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "hint":
        if session["exercise_type"] != "verbs":
            return
        prog      = session.get("_last_progress") or progress_line(session, item)
        v2        = item["v2"].split("/")[0]
        first     = v2[0].lower()
        hint_line = f"💡 V2 начинается на: *{first}...* ({len(v2)} букв)"
        if type_mode:
            text = (
                f"{prog}\n\n"
                f"🔤 *{item['v1']}*\n"
                f"🇷🇺 _{item['translation']}_\n\n"
                f"{hint_line}\n\n"
                f"Нажми *Написать*, чтобы ввести V2 и V3:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Написать", callback_data="type_answer")],
                [InlineKeyboardButton("⏹ Стоп",      callback_data="stop_session")],
            ])
        else:
            text = (
                f"{prog}\n\n"
                f"🔤 *{item['v1']}*\n"
                f"🇷🇺 _{item['translation']}_\n\n"
                f"{hint_line}\n\n"
                f"Вспомни V2 и V3, затем проверь:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👁 Показать ответ", callback_data="show")],
                [InlineKeyboardButton("⏹ Стоп",            callback_data="stop_session")],
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

    elif data == "type_answer":
        session["awaiting_input"] = True
        text, kb = build_type_prompt(session)
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "cancel_type":
        session["awaiting_input"] = False
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "next":
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = context.user_data.get("session")
    if not session or not session.get("awaiting_input"):
        return

    chat_id   = update.effective_chat.id
    type_mode = context.user_data.get("type_mode", False)
    item      = current_item(session)
    if not item:
        return

    session["awaiting_input"] = False
    try:
        await update.message.delete()
    except Exception:
        pass
    raw   = update.message.text.strip()
    parts = raw.lower().replace("/", " ").split()

    expected_v2 = item["v2"].lower().replace("/", " ").split()
    expected_v3 = item["v3"].lower().replace("/", " ").split()
    correct = len(parts) >= 2 and parts[0] in expected_v2 and parts[-1] in expected_v3

    text, kb = build_type_result(item, raw, correct)
    if correct:
        mark_known(session, item)
    else:
        mark_unknown(session, item)
    try:
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)
    except BadRequest:
        pass


# ─── Entry point ──────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Register the slash-command menu shown in the Telegram UI."""
    await app.bot.set_my_commands([
        BotCommand("start",   "Главное меню"),
        BotCommand("stats",   "Статистика"),
        BotCommand("weak",    "Сложные карточки"),
        BotCommand("history", "История сессий"),
        BotCommand("mode",    "Режим ввода (для глаголов)"),
        BotCommand("help",    "Помощь"),
    ])


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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    logger.info("Bot is running… (state: %s)", state_path)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
