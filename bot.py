import asyncio
import os
import random
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
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

TYPE_EMOJI = {
    "verbs":   "🔤",
    "prep":    "📍",
    "vp":      "➕",
    "adjprep": "🔗",
}

TYPE_LABEL = {
    "verbs":   "Неправильные глаголы",
    "prep":    "Предлоги in / on / at",
    "vp":      "Глаголы + to / -ing",
    "adjprep": "Прилагательные + предлог",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def item_id(item: dict) -> str:
    return item.get("v1") or item.get("verb") or item.get("adjective") or item.get("sentence", "?")


def _streak_label(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:   return "день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return "дня"
    return "дней"


def _card_plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:   return "карточку"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return "карточки"
    return "карточек"


# ─── Session ──────────────────────────────────────────────────────────────────

def new_session(exercise_type: str, size: int | None = None,
                user_id: int | None = None) -> dict:
    deck = CONTENT[exercise_type].copy()
    random.shuffle(deck)
    if size:
        deck = deck[:size]
    return {
        "exercise_type":  exercise_type,
        "queue":          deck,
        "pos":            0,
        "original_total": len(deck),
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

    if session["phase"] == "end_review":
        pos   = session["pos"] + 1
        total = len(session["queue"])
        return f"🔄 *Повторение {pos} / {total}*"

    iid    = item_id(item)
    is_new = iid not in session["first_shown"]
    done   = len(session["first_shown"])
    total  = session["original_total"]

    if is_new:
        return f"{emoji} *{done + 1} / {total}*"
    return f"🔄 *Повтор · {done} / {total}*"


# ─── Selectors ────────────────────────────────────────────────────────────────

def build_type_selector() -> tuple[str, InlineKeyboardMarkup]:
    text = "📚 *Что хочешь потренировать?*"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤 Неправильные глаголы",       callback_data="type_verbs")],
        [InlineKeyboardButton("📍 Предлоги in / on / at",      callback_data="type_prep")],
        [InlineKeyboardButton("➕ Глаголы + to / -ing",        callback_data="type_vp")],
        [InlineKeyboardButton("🔗 Прилагательные + предлог",   callback_data="type_adjprep")],
    ])
    return text, kb


def build_size_selector(exercise_type: str, type_mode: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    total = len(CONTENT[exercise_type])
    text  = f"🎯 *{TYPE_LABEL[exercise_type]}*\n\nСколько карточек?"
    row   = [InlineKeyboardButton("10", callback_data="size_10")]
    if total >= 20:
        row.append(InlineKeyboardButton("20", callback_data="size_20"))
    row.append(InlineKeyboardButton(f"Все {total}", callback_data="size_all"))

    rows = [row]
    if exercise_type == "verbs":
        mode_label = "✏️ Режим ввода: вкл ✓" if type_mode else "✏️ Режим ввода: выкл"
        rows.append([InlineKeyboardButton(mode_label, callback_data="toggle_mode")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="back_to_types")])
    return text, InlineKeyboardMarkup(rows)


# ─── Card builders ────────────────────────────────────────────────────────────

def build_verb_card(session: dict, type_mode: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"🔤 *{item['v1']}*\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"Помнишь все три формы?"
    )
    if type_mode:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Знаю",    callback_data="remember"),
                InlineKeyboardButton("❌ Не знаю", callback_data="forget"),
            ],
            [InlineKeyboardButton("✏️ Написать V2 и V3", callback_data="type_answer")],
            [InlineKeyboardButton("⏹ Стоп",              callback_data="stop_session")],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Помню",    callback_data="remember"),
                InlineKeyboardButton("❌ Не помню", callback_data="forget"),
            ],
            [
                InlineKeyboardButton("👁 Показать", callback_data="show"),
                InlineKeyboardButton("⏹ Стоп",      callback_data="stop_session"),
            ],
        ])
    return text, kb


def build_prep_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item     = current_item(session)
    sentence = item["sentence"].replace("{?}", "___")
    text = (
        f"{progress_line(session, item)}\n\n"
        f"{sentence}\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"Выбери правильный предлог:"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("in",  callback_data="ans_in"),
            InlineKeyboardButton("on",  callback_data="ans_on"),
            InlineKeyboardButton("at",  callback_data="ans_at"),
            InlineKeyboardButton("⏹",  callback_data="stop_session"),
        ],
    ])
    return text, kb


def build_vp_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"🔤 *{item['verb']}*\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"Какой шаблон?"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("+ -ing", callback_data="ans_ing"),
        InlineKeyboardButton("+ to",   callback_data="ans_to"),
        InlineKeyboardButton("⏹",     callback_data="stop_session"),
    ]])
    return text, kb


def build_adjprep_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item    = current_item(session)
    options = item["options"].copy()
    random.shuffle(options)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"🔤 *{item['adjective']}* + ?\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"Выбери правильный предлог:"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(opt, callback_data=f"ans_{opt}")
        for opt in options
    ] + [InlineKeyboardButton("⏹", callback_data="stop_session")]])
    return text, kb


def build_choice_result(item: dict, chosen: str, correct: bool) -> tuple[str, InlineKeyboardMarkup | None]:
    kb_next = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Следующая карточка", callback_data="next")]])

    if "sentence" in item:
        full = item["sentence"].replace("{?}", f"*{item['answer']}*")
        if correct:
            return f"✅ *Верно!*\n\n{full}\n\n📖 _{item['rule']}_", None
        return (
            f"❌ *Неверно.* Правильный ответ: *{item['answer']}*\n\n"
            f"{full}\n\n📖 _{item['rule']}_",
            kb_next,
        )

    if "adjective" in item:
        if correct:
            return (
                f"✅ *Верно!*\n\n"
                f"*{item['adjective']}* + *{item['preposition']}*\n\n"
                f"💬 _{item['example']}_",
                None,
            )
        return (
            f"❌ *Неверно.* Правильный ответ: *{item['adjective']} {item['preposition']}*\n\n"
            f"💬 _{item['example']}_",
            kb_next,
        )

    # verb pattern
    if correct:
        return (
            f"✅ *Верно!*\n\n"
            f"*{item['verb']}* + *{item['pattern']}*\n\n"
            f"💬 _{item['example']}_",
            None,
        )
    return (
        f"❌ *Неверно.* Правильный ответ: *+ {item['pattern']}*\n\n"
        f"*{item['verb']}* + *{item['pattern']}*\n"
        f"💬 _{item['example']}_",
        kb_next,
    )


def build_verb_answer(session: dict, source: str) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"📌 *V1:* `{item['v1']}`\n"
        f"📝 *V2:* `{item['v2']}`\n"
        f"✅ *V3:* `{item['v3']}`\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"💬 _{item['example']}_"
    )
    if source == "forget":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Следующая карточка", callback_data="next")]])
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Знал(а)",    callback_data="knew"),
            InlineKeyboardButton("❌ Не знал(а)", callback_data="didnt_know"),
        ]])
    return text, kb


def build_type_prompt(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    item = current_item(session)
    text = (
        f"{progress_line(session, item)}\n\n"
        f"✍️ *{item['v1']}* — _{item['translation']}_\n\n"
        f"Напиши V2 и V3 через пробел:"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_type")]])
    return text, kb


def build_type_result(item: dict, user_input: str, correct: bool) -> tuple[str, InlineKeyboardMarkup | None]:
    if correct:
        return (
            f"✅ *Верно!*\n\n"
            f"📌 *V1:* `{item['v1']}`\n"
            f"📝 *V2:* `{item['v2']}`\n"
            f"✅ *V3:* `{item['v3']}`\n\n"
            f"💬 _{item['example']}_",
            None,
        )
    return (
        f"❌ *Ты написал:* `{user_input}`\n\n"
        f"📌 *V1:* `{item['v1']}`\n"
        f"📝 *V2:* `{item['v2']}`\n"
        f"✅ *V3:* `{item['v3']}`\n"
        f"🇷🇺 _{item['translation']}_\n\n"
        f"💬 _{item['example']}_",
        InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Следующая карточка", callback_data="next")]]),
    )


def build_end_review_intro(count: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🏁 *Основная колода пройдена!*\n\n"
        f"Повторим *{count}* {_card_plural(count)}, которые вызвали затруднение.\n\n"
        f"Готов(а)?"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Начать повторение", callback_data="start_review")]])
    return text, kb


def build_final(session: dict, streak: int) -> tuple[str, InlineKeyboardMarkup]:
    results = session["results"]
    known   = sum(1 for v in results.values() if v)
    unknown = sum(1 for v in results.values() if not v)
    total   = len(results)
    pct     = round(known / total * 100) if total else 0
    ex_type = session["exercise_type"]

    if pct == 100:   grade = "🏆 Идеально! Ты знаешь всё!"
    elif pct >= 80:  grade = "🌟 Отличный результат!"
    elif pct >= 60:  grade = "📈 Хороший прогресс! Продолжай так!"
    elif pct >= 40:  grade = "💪 Не останавливайся, всё получится!"
    else:            grade = "📖 Регулярные повторения — ключ к успеху!"

    streak_line = f"🔥 Серия: *{streak} {_streak_label(streak)}* подряд\n" if streak else ""

    unknown_ids   = [iid for iid, ok in results.items() if not ok]
    unknown_block = ""
    if unknown_ids:
        lines = []
        for iid in sorted(unknown_ids):
            if ex_type == "verbs" and iid in VERBS_BY_V1:
                v = VERBS_BY_V1[iid]
                lines.append(f"• `{v['v1']}` → `{v['v2']}` / `{v['v3']}`")
            elif ex_type == "vp" and iid in VP_BY_VERB:
                vp = VP_BY_VERB[iid]
                lines.append(f"• `{vp['verb']}` + `{vp['pattern']}`")
            elif ex_type == "adjprep" and iid in ADJ_BY_ADJ:
                a = ADJ_BY_ADJ[iid]
                lines.append(f"• `{a['adjective']}` + `{a['preposition']}`")
            elif ex_type == "prep" and iid in PREP_BY_SENT:
                p = PREP_BY_SENT[iid]
                lines.append(f"• _{iid.replace('{?}', f'[{p[\"answer\"]}]')}_")
        if lines:
            unknown_block = "\n\n📋 *Повтори:*\n" + "\n".join(lines)

    text = (
        f"🎉 *Сессия завершена!*\n\n"
        f"✅ Знаю:        *{known}* {_card_plural(known)}\n"
        f"❌ Учу:          *{unknown}* {_card_plural(unknown)}\n"
        f"📊 Результат: *{pct}%*\n"
        f"{streak_line}"
        f"\n{grade}"
        f"{unknown_block}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Начать заново", callback_data="new_session")],
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

    session["first_shown"].add(item_id(item))
    await safe_edit(bot, chat_id, session["message_id"], text, kb)


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
        streak  = db.save_session(user_id, known, unknown, len(results), results)

    text, kb = build_final(session, streak)
    await safe_edit(bot, chat_id, session["message_id"], text, kb)


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.ensure_user(update.effective_user.id)

    try:
        await update.message.delete()
    except Exception:
        pass

    text, kb = build_type_selector()
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
    session = context.user_data.get("session")
    user_id = update.effective_user.id
    streak  = db.get_streak(user_id)
    streak_line = f"🔥 Серия: *{streak} {_streak_label(streak)}*\n" if streak else ""

    if not session or not session["results"]:
        await update.message.reply_text(
            f"Пока нет данных. Начни сессию с /start 🙂\n{streak_line}",
            parse_mode="Markdown",
        )
        return

    results = session["results"]
    known   = sum(1 for v in results.values() if v)
    unknown = sum(1 for v in results.values() if not v)
    studied = len(results)
    total   = session["original_total"]

    await update.message.reply_text(
        f"📊 *Статистика сессии*\n\n"
        f"✅ Знаю:           *{known}*\n"
        f"❌ Учу:             *{unknown}*\n"
        f"📚 Пройдено:   *{studied} / {total}*\n"
        f"⏳ Осталось:   *{total - studied}*\n"
        f"{streak_line}\n"
        f"_Продолжай, у тебя всё получается!_",
        parse_mode="Markdown",
    )


async def cmd_weak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.get_weak_verbs(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Пока нет данных. Пройди хотя бы одну сессию до конца! 📚")
        return
    lines = [_format_weak_item(r["verb_v1"], r["unknown_count"]) for r in rows]
    await update.message.reply_text(
        "📋 *Твои сложные карточки:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.get_history(update.effective_user.id)
    if not rows:
        await update.message.reply_text("История сессий пуста. Пройди первую сессию! 📚")
        return
    lines = []
    for r in rows:
        pct = round(r["known"] / r["total"] * 100) if r["total"] else 0
        lines.append(f"📅 {r['finished_at']} — {r['known']}/{r['total']} ({pct}%)")
    await update.message.reply_text(
        "📈 *История сессий:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


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
    await update.message.reply_text(
        "📚 *Study English Bot*\n\n"
        "*Типы тренировок:*\n"
        "🔤 Неправильные глаголы — вспомни V1/V2/V3\n"
        "📍 Предлоги — in, on или at?\n"
        "➕ Глаголы + to/-ing — enjoy swimming или want to go?\n"
        "🔗 Прилагательные — afraid of, nervous about?\n\n"
        "*Команды:*\n"
        "/start — новая сессия\n"
        "/stats — статистика текущей сессии\n"
        "/weak — твои самые сложные карточки\n"
        "/history — история последних сессий\n"
        "/mode — переключить режим ввода (для глаголов)\n"
        "/help — эта справка\n\n"
        "*Совет:* режим ввода можно включить прямо в меню выбора размера колоды.",
        parse_mode="Markdown",
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        return
    s = db.get_admin_stats()
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


# ─── Shared weak-item formatter ───────────────────────────────────────────────

def _format_weak_item(iid: str, error_count: int) -> str:
    if iid in VERBS_BY_V1:
        return f"• `{iid}` (глагол) — ошибок: {error_count}"
    if iid in ADJ_BY_ADJ:
        a = ADJ_BY_ADJ[iid]
        return f"• `{iid}` + {a['preposition']} — ошибок: {error_count}"
    if iid in VP_BY_VERB:
        vp = VP_BY_VERB[iid]
        return f"• `{iid}` + {vp['pattern']} — ошибок: {error_count}"
    short = iid[:40] + "…" if len(iid) > 40 else iid
    return f"• _{short}_ — ошибок: {error_count}"


# ─── Callback handler ─────────────────────────────────────────────────────────

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    await query.answer()
    data      = query.data
    chat_id   = query.message.chat_id
    user_id   = query.from_user.id
    type_mode = context.user_data.get("type_mode", False)

    # ── Stop session ──
    if data == "stop_session":
        session = context.user_data.pop("session", None)
        text, kb = build_type_selector()
        if session and session["results"]:
            done  = len(session["results"])
            known = sum(1 for v in session["results"].values() if v)
            note  = f"_Сессия прервана — {known} из {done} {_card_plural(done)} выполнено_\n\n"
            text  = note + text
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
        text, kb = build_size_selector(ex_type, type_mode=context.user_data["type_mode"])
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data.startswith("type_"):
        ex_type = data[5:]  # "verbs", "prep", "vp", "adjprep"
        context.user_data["pending_type"] = ex_type
        text, kb = build_size_selector(ex_type, type_mode=type_mode)
        await safe_edit(context.bot, chat_id, query.message.message_id, text, kb)
        return

    if data in ("size_10", "size_20", "size_all"):
        ex_type = context.user_data.get("pending_type", "verbs")
        size_map = {"size_10": 10, "size_20": 20, "size_all": None}
        session  = new_session(ex_type, size=size_map[data], user_id=user_id)
        msg_id   = context.user_data.get("card_message_id") or query.message.message_id
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

    # ── Final screen — inline stats ──
    if data == "final_weak":
        rows = db.get_weak_verbs(user_id)
        if not rows:
            body = "Данных пока нет — пройди больше сессий!"
        else:
            body = "\n".join(_format_weak_item(r["verb_v1"], r["unknown_count"]) for r in rows)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Назад к результатам", callback_data="back_to_final")]])
        await safe_edit(context.bot, chat_id, query.message.message_id,
                        f"📋 *Сложные карточки:*\n\n{body}", kb)
        return

    if data == "final_history":
        rows = db.get_history(user_id)
        if not rows:
            body = "История пуста."
        else:
            lines = []
            for r in rows:
                pct = round(r["known"] / r["total"] * 100) if r["total"] else 0
                lines.append(f"📅 {r['finished_at']} — {r['known']}/{r['total']} ({pct}%)")
            body = "\n".join(lines)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Назад к результатам", callback_data="back_to_final")]])
        await safe_edit(context.bot, chat_id, query.message.message_id,
                        f"📈 *История сессий:*\n\n{body}", kb)
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
            await safe_edit(context.bot, chat_id, session["message_id"], text, InlineKeyboardMarkup([]))
            await asyncio.sleep(1.2)
            advance(session)
            await show_card(chat_id, session, context.bot, type_mode=type_mode)
        else:
            mark_unknown(session, item)
            await safe_edit(context.bot, chat_id, session["message_id"], text, kb)
        return

    # ── Verb callbacks ──
    if data == "type_answer":
        session["awaiting_input"] = True
        text, kb = build_type_prompt(session)
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "cancel_type":
        session["awaiting_input"] = False
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "remember":
        mark_known(session, item)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "forget":
        mark_unknown(session, item)
        text, kb = build_verb_answer(session, "forget")
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "show":
        text, kb = build_verb_answer(session, "show")
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "next":
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "knew":
        mark_known(session, item)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "didnt_know":
        mark_unknown(session, item)
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
    raw   = update.message.text.strip()
    parts = raw.lower().replace("/", " ").split()

    expected_v2 = item["v2"].lower().replace("/", " ").split()
    expected_v3 = item["v3"].lower().replace("/", " ").split()
    correct = len(parts) >= 2 and parts[0] in expected_v2 and parts[-1] in expected_v3

    text, kb = build_type_result(item, raw, correct)
    if correct:
        mark_known(session, item)
        try:
            await safe_edit(context.bot, chat_id, session["message_id"], text, InlineKeyboardMarkup([]))
        except BadRequest:
            pass
        await asyncio.sleep(1.5)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
    else:
        mark_unknown(session, item)
        try:
            await safe_edit(context.bot, chat_id, session["message_id"], text, kb)
        except BadRequest:
            pass


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    db.init_db()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("weak",    cmd_weak))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("mode",    cmd_mode))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("admin",   cmd_admin))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
