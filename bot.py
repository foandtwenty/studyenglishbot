import os
import random
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest

from verbs import VERBS

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─── Session ──────────────────────────────────────────────────────────────────

def new_session() -> dict:
    deck = VERBS.copy()
    random.shuffle(deck)
    return {
        "queue":          deck,        # working list of verb dicts (may grow via re-inserts)
        "pos":            0,           # current index in queue
        "original_total": len(deck),  # never changes — used for the X/Y counter
        "first_shown":    set(),       # v1s seen for the first time (tracks unique progress)
        "results":        {},          # v1 -> True (known) | False (unknown)
        "review_buffer":  [],          # [(verb_dict, countdown), …] — mid-session re-reviews
        "end_review":     [],          # verbs queued for the final pass
        "phase":          "main",      # "main" | "end_review"
        "message_id":     None,
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


def build_card(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    verb = current_verb(session)
    text = (
        f"{progress_line(session, verb)}\n\n"
        f"🔤 *{verb['v1']}*\n"
        f"🇷🇺 _{verb['translation']}_\n\n"
        f"Помнишь все три формы?"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Помню",    callback_data="remember"),
            InlineKeyboardButton("❌ Не помню", callback_data="forget"),
        ],
        [InlineKeyboardButton("👁 Показать",   callback_data="show")],
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


def build_final(session: dict) -> tuple[str, InlineKeyboardMarkup]:
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

    text = (
        f"🎉 *Сессия завершена!*\n\n"
        f"✅ Знаю:        *{known}* глагол(ов)\n"
        f"❌ Учу:          *{unknown}* глагол(ов)\n"
        f"📊 Результат: *{pct}%*\n\n"
        f"{grade}\n\n"
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

    # Schedule re-review in 2–3 cards (avoid duplicate in buffer)
    if not any(v["v1"] == v1 for v, _ in session["review_buffer"]):
        session["review_buffer"].append((verb, random.randint(2, 3)))

    # Add to end-of-session pass (avoid duplicate)
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


async def show_card(chat_id: int, session: dict, bot) -> None:
    verb = current_verb(session)
    if not verb:
        await show_results(chat_id, session, bot)
        return

    # Track first-time display for progress counter
    session["first_shown"].add(verb["v1"])

    text, kb = build_card(session)
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

    text, kb = build_final(session)
    await safe_edit(bot, chat_id, session["message_id"], text, kb)


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = new_session()
    context.user_data["session"] = session

    msg = await update.message.reply_text("⏳ Загружаем карточки…")
    session["message_id"] = msg.message_id
    await show_card(update.effective_chat.id, session, context.bot)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = context.user_data.get("session")

    if not session:
        await update.message.reply_text(
            "Пока нет данных. Начни сессию с /start 🙂",
            parse_mode="Markdown",
        )
        return

    results = session["results"]
    known   = sum(1 for v in results.values() if v)
    unknown = sum(1 for v in results.values() if not v)
    studied = len(results)
    total   = session["original_total"]
    left    = total - studied

    text = (
        f"📊 *Статистика сессии*\n\n"
        f"✅ Знаю:           *{known}*\n"
        f"❌ Учу:             *{unknown}*\n"
        f"📚 Пройдено:   *{studied} / {total}*\n"
        f"⏳ Осталось:   *{left}*\n\n"
        f"_Продолжай, у тебя всё получается!_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ─── Callback handler ─────────────────────────────────────────────────────────

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    if data == "new_session":
        session = new_session()
        session["message_id"] = query.message.message_id
        context.user_data["session"] = session
        await show_card(chat_id, session, context.bot)
        return

    session = context.user_data.get("session")

    if data == "start_review":
        if session:
            await show_card(chat_id, session, context.bot)
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

    if data == "remember":
        mark_known(session, verb)
        advance(session)
        await show_card(chat_id, session, context.bot)

    elif data == "forget":
        mark_unknown(session, verb)
        await show_answer(chat_id, session, context.bot, "forget")

    elif data == "show":
        await show_answer(chat_id, session, context.bot, "show")

    elif data == "next":
        advance(session)
        await show_card(chat_id, session, context.bot)

    elif data == "knew":
        mark_known(session, verb)
        advance(session)
        await show_card(chat_id, session, context.bot)

    elif data == "didnt_know":
        mark_unknown(session, verb)
        advance(session)
        await show_card(chat_id, session, context.bot)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_button))

    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
