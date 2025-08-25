import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo


import aiosqlite
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    JobQueue
)

# ==================== НАСТРОЙКИ ====================
TZ = ZoneInfo("Europe/Istanbul")  # для Н.Новгорода совпадает с МСК (UTC+3)
MORNING_REMIND_AT = time(9, 0, tzinfo=TZ)
MORNING_DEADLINE = time(10, 0, tzinfo=TZ)
MORNING_ESCALATE_AT = time(10, 5, tzinfo=TZ)

EVENING_REMIND_AT = time(20, 30, tzinfo=TZ)
EVENING_DEADLINE = time(21, 0, tzinfo=TZ)
EVENING_ESCALATE_AT = time(21, 5, tzinfo=TZ)

DB_PATH = "reminder_bot.db"

REMINDER_TEXT = (
    "⚠️ *Ежедневный отчёт по регламенту.*\n\n"
    "Пожалуйста, пришлите в чат: видео форсунок + короткий текст (что работает / что нет, критичность, что нужно).\n"
    "После отправки ответьте одним словом: *отчет* (или *отчёт*) — бот зафиксирует выполнение."
)

LATE_TEXT_TMPL = (
    "⏰ *Просрочка отчёта* — {period_ru} (дедлайн был {deadline}).\n"
    "Нужно прислать видео + текст и ответить словом *отчет*."
)

OK_TEXT_TMPL = "✅ Отчёт за {period_ru} принят. Спасибо!"

HELP_TEXT = (
    "Я напоминаю прислать отчёт утром (до 10:00) и вечером (до 21:00).\n\n"
    "Команды:\n"
    "• /here — привязать этот чат как целевой\n"
    "• /status — статус за сегодня\n"
    "• /help — помощь"
)

# ==================== УТИЛИТЫ ======================
@dataclass
class Period:
    key: str  # 'morning' | 'evening'
    ru: str   # 'утро' | 'вечер'
    remind_at: time
    deadline: time
    escalate_at: time


def get_periods():
    return [
        Period("morning", "утро", MORNING_REMIND_AT, MORNING_DEADLINE, MORNING_ESCALATE_AT),
        Period("evening", "вечер", EVENING_REMIND_AT, EVENING_DEADLINE, EVENING_ESCALATE_AT),
        ]


def now_tz() -> datetime:
    return datetime.now(TZ)


def pick_current_period(dt: datetime) -> Period:
    """Определяем, к какому периоду отнести сообщение сейчас."""
    t = dt.timetz()
    if t <= EVENING_DEADLINE:
        # до 21:00 считаем текущим утро, если до 14:00; иначе вечер.
        # Но проще: до 14:00 — утро, после — вечер.
        if t <= time(14, 0, tzinfo=TZ):
            return get_periods()[0]
        return get_periods()[1]
    return get_periods()[1]


# ==================== ХРАНИЛИЩЕ ====================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS reports (
  d TEXT NOT NULL,          -- YYYY-MM-DD
  period TEXT NOT NULL,     -- 'morning' | 'evening'
  reported_by INTEGER,      -- user id
  message_id INTEGER,       -- message id
  ts TEXT NOT NULL,         -- ISO datetime
  PRIMARY KEY (d, period)
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()


async def get_report(d: date, period_key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT d, period, reported_by, message_id, ts FROM reports WHERE d=? AND period=?",
            (d.isoformat(), period_key),
        )
        return await cur.fetchone()


async def set_report(d: date, period_key: str, user_id: int, message_id: int, ts: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO reports(d,period,reported_by,message_id,ts) VALUES(?,?,?,?,?)",
            (d.isoformat(), period_key, user_id, message_id, ts.isoformat()),
        )
        await db.commit()


# ==================== ХЕНДЛЕРЫ =====================
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text("Команду /here нужно отправить *в групповом чате*, где будут отчёты.", parse_mode=ParseMode.MARKDOWN)
        return
    await set_setting("chat_id", str(chat.id))
    await update.effective_message.reply_text("✅ Этот чат сохранён как целевой для напоминаний.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = await get_setting("chat_id")
    if not chat_id:
        await update.effective_message.reply_text("Чат ещё не привязан. Отправьте /here в нужном чате.")
        return
    d = now_tz().date()
    m = await get_report(d, "morning")
    e = await get_report(d, "evening")

    def fmt(row):
        if not row:
            return "— нет"
        ts = row[4]
        uid = row[2]
        mid = row[3]
        return f"✅ есть (от <code>{uid}</code>, {ts}) [msg:{mid}]"

    txt = (
        f"Статус за {d.isoformat()}\n\n"
        f"Утро (до 10:00): {fmt(m)}\n"
        f"Вечер (до 21:00): {fmt(e)}"
    )
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Засчитываем отчёт по ключевому слову
    msg = update.effective_message
    chat = update.effective_chat
    text = (msg.text or "").lower()
    if "отчет" not in text and "отчёт" not in text:
        return

    # Только в целевом чате
    target_chat_id = await get_setting("chat_id")
    if not target_chat_id or str(chat.id) != target_chat_id:
        return

    dt = now_tz()
    per = pick_current_period(dt)
    d = dt.date()

    already = await get_report(d, per.key)
    if already:
        # Уже есть — вежливо подтверждаем, но без повторной записи
        await msg.reply_text(OK_TEXT_TMPL.format(period_ru=per.ru))
        return

    await set_report(d, per.key, msg.from_user.id if msg.from_user else 0, msg.message_id, dt)
    await msg.reply_text(OK_TEXT_TMPL.format(period_ru=per.ru))


# ==================== ДЖОБЫ =======================
async def send_reminder(context: ContextTypes.DEFAULT_TYPE, per: Period):
    chat_id = await get_setting("chat_id")
    if not chat_id:
        return
    d = now_tz().date()
    already = await get_report(d, per.key)
    if not already:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"{REMINDER_TEXT}\n\nПериод: *{per.ru}* (дедлайн {per.deadline.strftime('%H:%M')}).",
            parse_mode=ParseMode.MARKDOWN,
        )


async def escalate_if_missing(context: ContextTypes.DEFAULT_TYPE, per: Period):
    chat_id = await get_setting("chat_id")
    if not chat_id:
        return
    d = now_tz().date()
    already = await get_report(d, per.key)
    if not already:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=LATE_TEXT_TMPL.format(period_ru=per.ru, deadline=per.deadline.strftime('%H:%M')),
            parse_mode=ParseMode.MARKDOWN,
        )


def next_run_datetime(t: time) -> datetime:
    now = now_tz()
    candidate = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def scheduler(app):
    # Планируем одиночные задачи на ближайший запуск, и после выполнения — пересоздаём на следующий день
    def schedule_period(per: Period):
        # reminder
        async def reminder_job(ctx: ContextTypes.DEFAULT_TYPE):
            await send_reminder(ctx, per)
            # перепланировать на завтра
            when = next_run_datetime(per.remind_at)
            app.job_queue.run_once(reminder_job, when, name=f"remind_{per.key}")
        # escalate
        async def escalate_job(ctx: ContextTypes.DEFAULT_TYPE):
            await escalate_if_missing(ctx, per)
            when = next_run_datetime(per.escalate_at)
            app.job_queue.run_once(escalate_job, when, name=f"late_{per.key}")

        app.job_queue.run_once(reminder_job, next_run_datetime(per.remind_at), name=f"remind_{per.key}")
        app.job_queue.run_once(escalate_job, next_run_datetime(per.escalate_at), name=f"late_{per.key}")

    for per in get_periods():
        schedule_period(per)


# ==================== MAIN ========================
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Установите переменную окружения BOT_TOKEN")

    asyncio.run(init_db())

    app = ApplicationBuilder().token(token).build()
    jq = app.job_queue
    if jq is None:
        jq = JobQueue()
        jq.set_application(app)
        jq.start()
    # Команды
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("here", cmd_here))
    app.add_handler(CommandHandler("status", cmd_status))

    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))

    # Планировщик
    scheduler(app)

    # Ensure an event loop exists for Python 3.13
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    print("Bot started. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        print("Bye!")
