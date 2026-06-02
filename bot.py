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
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

VERBS_BY_V1 = {v["v1"]: v for v in VERBS}


# ─── Session ──────────────────────────────────────────────────────────────────

def new_session(size: int | None = None, user_id: int | None = None) -> dict:
    deck = VERBS.copy()
    random.shuffle(deck)
    if size:
        deck = deck[:size]
    return {
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


def current_verb(session: dict) -> dict | None:
    q, p = session["queue"], session["pos"]
    return q[p] if p < len(q) else None


# ─── Text / keyboard builders ─────────────────────────────────────────────────

def progress_line(session: dict, verb: dict) -> str:
    if session["phase"] == "end_review":
        pos   = session["pos"] + 1
        total = len(session["queue"])
        return f"🔄 *Повторение {pos} / {total}*"

    is_new = verb["v1"] not in session["first_shown"]
    done   = len(session["first_shown"])
    total  = session["original_total"]

    if is_new:
        return f"📚 *{done + 1} / {total}*"
    return f"🔄 *Повтор · {done} / {total}*"


def build_size_selector() -> tuple[str, InlineKeyboardMarkup]:
    text = "📚 *Сколько глаголов хочешь повторить?*"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("10",     callback_data="size_10"),
        InlineKeyboardButton("20",     callback_data="size_20"),
        InlineKeyboardButton(f"Все {len(VERBS)}", callback_data="size_all"),
    ]])
    return text, kb


def build_card(session: dict, type_mode: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    verb = current_verb(session)
    text = (
        f"{progress_line(session, verb)}\n\n"
        f"🔤 *{verb['v1']}*\n"
        f"🇷🇺 _{verb['translation']}_\n\n"
        f"Помнишь все три формы?"
    )
    if type_mode:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Знаю",    callback_data="remember"),
                InlineKeyboardButton("❌ Не знаю", callback_data="forget"),
            ],
            [InlineKeyboardButton("✏️ Написать V2 и V3", callback_data="type_answer")],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Помню",    callback_data="remember"),
                InlineKeyboardButton("❌ Не помню", callback_data="forget"),
            ],
            [InlineKeyboardButton("👁 Показать",   callback_data="show")],
        ])
    return text, kb


def build_type_prompt(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    verb = current_verb(session)
    text = (
        f"{progress_line(session, verb)}\n\n"
        f"✍️ *{verb['v1']}* — _{verb['translation']}_\n\n"
        f"Напиши V2 и V3 через пробел:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_type")]
    ])
    return text, kb


def build_type_result(verb: dict, user_input: str, correct: bool) -> tuple[str, InlineKeyboardMarkup | None]:
    if correct:
        text = (
            f"✅ *Верно!*\n\n"
            f"📌 *V1:* `{verb['v1']}`\n"
            f"📝 *V2:* `{verb['v2']}`\n"
            f"✅ *V3:* `{verb['v3']}`\n\n"
            f"💬 _{verb['example']}_"
        )
        return text, None
    text = (
        f"❌ *Ты написал:* `{user_input}`\n\n"
        f"📌 *V1:* `{verb['v1']}`\n"
        f"📝 *V2:* `{verb['v2']}`\n"
        f"✅ *V3:* `{verb['v3']}`\n"
        f"🇷🇺 _{verb['translation']}_\n\n"
        f"💬 _{verb['example']}_"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Следующая карточка", callback_data="next")]
    ])
    return text, kb


def build_answer(session: dict, source: str) -> tuple[str, InlineKeyboardMarkup]:
    verb = current_verb(session)
    text = (
        f"{progress_line(session, verb)}\n\n"
        f"📌 *V1:* `{verb['v1']}`\n"
        f"📝 *V2:* `{verb['v2']}`\n"
        f"✅ *V3:* `{verb['v3']}`\n"
        f"🇷🇺 _{verb['translation']}_\n\n"
        f"💬 _{verb['example']}_"
    )
    if source == "forget":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ Следующая карточка", callback_data="next")]
        ])
    else:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Знал(а)",    callback_data="knew"),
                InlineKeyboardButton("❌ Не знал(а)", callback_data="didnt_know"),
            ]
        ])
    return text, kb


def build_end_review_intro(count: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🏁 *Основная колода пройдена!*\n\n"
        f"Повторим *{count}* глагол(а/ов), которые вызвали затруднение.\n\n"
        f"Готов(а)?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Начать повторение", callback_data="start_review")]
    ])
    return text, kb


def _streak_label(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "дня"
    return "дней"


def build_final(session: dict, streak: int) -> tuple[str, InlineKeyboardMarkup]:
    results = session["results"]
    known   = sum(1 for v in results.values() if v)
    unknown = sum(1 for v in results.values() if not v)
    total   = len(results)
    pct     = round(known / total * 100) if total else 0

    if pct == 100:   grade = "🏆 Идеально! Ты знаешь все глаголы!"
    elif pct >= 80:  grade = "🌟 Отличный результат!"
    elif pct >= 60:  grade = "📈 Хороший прогресс! Продолжай так!"
    elif pct >= 40:  grade = "💪 Не останавливайся, всё получится!"
    else:            grade = "📖 Регулярные повторения — ключ к успеху!"

    streak_line = f"🔥 Серия: *{streak} {_streak_label(streak)}* подряд\n" if streak > 0 else ""

    unknown_verbs = sorted(
        [VERBS_BY_V1[v1] for v1, ok in results.items() if not ok and v1 in VERBS_BY_V1],
        key=lambda v: v["v1"],
    )
    unknown_block = ""
    if unknown_verbs:
        lines = "\n".join(
            f"• `{v['v1']}` → `{v['v2']}` / `{v['v3']}`"
            for v in unknown_verbs
        )
        unknown_block = f"\n\n📋 *Глаголы для изучения:*\n{lines}"

    text = (
        f"🎉 *Сессия завершена!*\n\n"
        f"✅ Знаю:        *{known}* глагол(ов)\n"
        f"❌ Учу:          *{unknown}* глагол(ов)\n"
        f"📊 Результат: *{pct}%*\n"
        f"{streak_line}"
        f"\n{grade}"
        f"{unknown_block}\n\n"
        f"_/start — новая сессия · /stats — статистика_"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Начать заново", callback_data="new_session")]
    ])
    return text, kb


# ─── Session mutations ────────────────────────────────────────────────────────

def mark_known(session: dict, verb: dict) -> None:
    session["results"][verb["v1"]] = True


def mark_unknown(session: dict, verb: dict) -> None:
    session["results"][verb["v1"]] = False

    if session["phase"] != "main":
        return

    v1 = verb["v1"]
    if not any(v["v1"] == v1 for v, _ in session["review_buffer"]):
        session["review_buffer"].append((verb, random.randint(2, 3)))
    if not any(v["v1"] == v1 for v in session["end_review"]):
        session["end_review"].append(verb)


def advance(session: dict) -> None:
    session["pos"] += 1
    new_buf = []
    for verb, countdown in session["review_buffer"]:
        countdown -= 1
        if countdown <= 0:
            session["queue"].insert(session["pos"], verb)
        else:
            new_buf.append((verb, countdown))
    session["review_buffer"] = new_buf


# ─── Rendering helpers ────────────────────────────────────────────────────────

async def safe_edit(bot, chat_id: int, message_id: int,
                    text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def show_card(chat_id: int, session: dict, bot, type_mode: bool = False) -> None:
    verb = current_verb(session)
    if not verb:
        await show_results(chat_id, session, bot)
        return

    # Build BEFORE adding to first_shown so progress_line sees correct is_new state
    text, kb = build_card(session, type_mode=type_mode)
    session["first_shown"].add(verb["v1"])
    await safe_edit(bot, chat_id, session["message_id"], text, kb)


async def show_answer(chat_id: int, session: dict, bot, source: str) -> None:
    text, kb = build_answer(session, source)
    await safe_edit(bot, chat_id, session["message_id"], text, kb)


async def show_results(chat_id: int, session: dict, bot) -> None:
    if session["phase"] == "main" and session["end_review"]:
        deck = session["end_review"].copy()
        random.shuffle(deck)
        session.update({
            "phase":         "end_review",
            "queue":         deck,
            "pos":           0,
            "review_buffer": [],
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
    text, kb = build_size_selector()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


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

    text = (
        f"📊 *Статистика сессии*\n\n"
        f"✅ Знаю:           *{known}*\n"
        f"❌ Учу:             *{unknown}*\n"
        f"📚 Пройдено:   *{studied} / {total}*\n"
        f"⏳ Осталось:   *{total - studied}*\n"
        f"{streak_line}\n"
        f"_Продолжай, у тебя всё получается!_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_weak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.get_weak_verbs(update.effective_user.id)

    if not rows:
        await update.message.reply_text(
            "Пока нет данных. Пройди хотя бы одну сессию до конца! 📚"
        )
        return

    lines = [
        f"• `{r['verb_v1']}` — ошибок: {r['unknown_count']}"
        for r in rows
    ]
    await update.message.reply_text(
        "📋 *Твои сложные глаголы:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.get_history(update.effective_user.id)

    if not rows:
        await update.message.reply_text(
            "История сессий пуста. Пройди первую сессию! 📚"
        )
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
    if context.user_data["type_mode"]:
        msg = (
            "✏️ *Режим ввода включён*\n\n"
            "Теперь можно печатать V2 и V3 чтобы проверить себя.\n"
            "Кнопки «Знаю» и «Не знаю» тоже остаются."
        )
    else:
        msg = "👆 *Режим кнопок включён*\nВозвращаемся к стандартным карточкам."
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📚 *Study English Bot*\n\n"
        "Учи неправильные глаголы английского языка!\n\n"
        "*Команды:*\n"
        "/start — новая сессия\n"
        "/stats — статистика текущей сессии\n"
        "/weak — твои самые сложные глаголы\n"
        "/history — история последних сессий\n"
        "/mode — переключить режим (кнопки ↔ ввод текста)\n"
        "/help — эта справка\n\n"
        "*Как работает:*\n"
        "Тебе показывают глагол и просят вспомнить V2 и V3.\n"
        "• *Помню* — переход к следующей карточке\n"
        "• *Не помню* — показывает все формы, глагол вернётся\n"
        "• *Показать* — подсмотреть и оценить себя\n\n"
        "Забытые глаголы повторяются позже и в конце сессии.\n"
        "В режиме ввода пиши V2 и V3 через пробел, например: `broke broken`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ─── Callback handler ─────────────────────────────────────────────────────────

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    await query.answer()
    data      = query.data
    chat_id   = query.message.chat_id
    user_id   = query.from_user.id
    type_mode = context.user_data.get("type_mode", False)

    if data in ("size_10", "size_20", "size_all"):
        size_map = {"size_10": 10, "size_20": 20, "size_all": None}
        session = new_session(size=size_map[data], user_id=user_id)
        session["message_id"] = query.message.message_id
        context.user_data["session"] = session
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
        return

    if data == "new_session":
        text, kb = build_size_selector()
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=query.message.message_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except BadRequest:
            pass
        return

    session = context.user_data.get("session")

    if data == "start_review":
        if session:
            await show_card(chat_id, session, context.bot, type_mode=type_mode)
        return

    if not session:
        await query.edit_message_text(
            "Сессия не найдена. Нажми /start чтобы начать заново."
        )
        return

    verb = current_verb(session)
    if not verb:
        await show_results(chat_id, session, context.bot)
        return

    if data == "type_answer":
        session["awaiting_input"] = True
        text, kb = build_type_prompt(session)
        await safe_edit(context.bot, chat_id, session["message_id"], text, kb)

    elif data == "cancel_type":
        session["awaiting_input"] = False
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "remember":
        mark_known(session, verb)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "forget":
        mark_unknown(session, verb)
        await show_answer(chat_id, session, context.bot, "forget")

    elif data == "show":
        await show_answer(chat_id, session, context.bot, "show")

    elif data == "next":
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "knew":
        mark_known(session, verb)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)

    elif data == "didnt_know":
        mark_unknown(session, verb)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = context.user_data.get("session")
    if not session or not session.get("awaiting_input"):
        return

    chat_id   = update.effective_chat.id
    type_mode = context.user_data.get("type_mode", False)
    verb      = current_verb(session)
    if not verb:
        return

    session["awaiting_input"] = False
    raw = update.message.text.strip()

    # Normalize: split on spaces or slashes
    parts        = raw.lower().replace("/", " ").split()
    expected_v2  = verb["v2"].lower().replace("/", " ").split()
    expected_v3  = verb["v3"].lower().replace("/", " ").split()

    correct = (
        len(parts) >= 2
        and parts[0] in expected_v2
        and parts[-1] in expected_v3
    )

    text, kb = build_type_result(verb, raw, correct)

    if correct:
        mark_known(session, verb)
        try:
            await safe_edit(context.bot, chat_id, session["message_id"], text,
                            InlineKeyboardMarkup([]))
        except BadRequest:
            pass
        await asyncio.sleep(1.5)
        advance(session)
        await show_card(chat_id, session, context.bot, type_mode=type_mode)
    else:
        mark_unknown(session, verb)
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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
