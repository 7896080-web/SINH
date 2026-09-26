"""Telegram-обвязка: принимает апдейты, зовёт Flow, отправляет ответы.

Запуск: python -m finance (настройки — переменные окружения, см. README).
"""

import asyncio
import io
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .flow import Flow, Reply
from .recognize import ClaudeRecognizer, DEFAULT_MODEL
from .storage import Storage

log = logging.getLogger(__name__)

COMMANDS = ["start", "help", "cancel", "cards", "addcard", "delcard", "cats", "addcat",
            "list", "sverka", "done", "itog", "biz", "notbiz", "vypiska", "fix"]
MAX_TEXT = 4000  # лимит Telegram — 4096 символов на сообщение
MAX_FILE = 20 * 1024 * 1024  # больше бот скачать не может


def _split(text: str) -> list[str]:
    parts, chunk = [], ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > MAX_TEXT and chunk:
            parts.append(chunk)
            chunk = ""
        chunk += line + "\n"
    parts.append(chunk)
    return [p.rstrip("\n") for p in parts if p.strip()]


async def send(update: Update, replies: list[Reply]):
    chat = update.effective_chat
    for r in replies:
        chunks = _split(r.text) or [""]
        for i, chunk in enumerate(chunks):
            markup = None
            if r.buttons and i == len(chunks) - 1:
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                     for row in r.buttons])
            await chat.send_message(chunk, reply_markup=markup)
        if r.file:
            name, data = r.file
            await chat.send_document(InputFile(io.BytesIO(data), filename=name))


def build_app(token: str, flow: Flow, allowed: set[int]) -> Application:
    app = Application.builder().token(token).build()

    async def guard(update: Update) -> bool:
        user = update.effective_user
        if user and user.id in allowed:
            return True
        # Бот видит финансовые данные — чужим не отвечаем ничем, кроме id,
        # чтобы владелец мог добавить себя в ALLOWED_USER_IDS при настройке.
        if update.effective_chat and update.message:
            await update.effective_chat.send_message(
                f"Доступ закрыт. Ваш Telegram id: {user.id if user else '?'}")
        return False

    async def run(update: Update, fn, *args):
        await update.effective_chat.send_action(ChatAction.TYPING)
        # Распознавание — синхронный сетевой вызов на десятки секунд;
        # в отдельном потоке, чтобы не блокировать цикл событий.
        replies = await asyncio.to_thread(fn, update.effective_chat.id, *args)
        await send(update, replies)

    async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        command = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
        await run(update, flow.on_command, command, " ".join(context.args or []))

    async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        photo = update.message.photo[-1]  # самый крупный вариант
        tg_file = await photo.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        await run(update, flow.on_files, [(data, "image/jpeg")], update.message.caption or "")

    async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        doc = update.message.document
        if doc.file_size and doc.file_size > MAX_FILE:
            await update.effective_chat.send_message("Файл больше 20 МБ — Telegram не даст его скачать. "
                                                     "Пришлите частями или скриншотами.")
            return
        mime = doc.mime_type or ""
        name = (doc.file_name or "").lower()
        if name.endswith(".csv"):
            mime = "text/csv"
        elif name.endswith(".xlsx"):
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        await run(update, flow.on_files, [(data, mime)], update.message.caption or "", name)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        await run(update, flow.on_text, update.message.text)

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not await guard(update):
            return
        # Убираем кнопки у отвеченного вопроса, чтобы не нажать дважды.
        try:
            await query.edit_message_reply_markup(None)
        except Exception:  # сообщение могли удалить — не критично
            pass
        await run(update, flow.on_button, query.data)

    async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
        log.exception("Ошибка при обработке апдейта", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            await update.effective_chat.send_message(
                "Что-то пошло не так, запись не сохранена. Попробуйте ещё раз.")

    app.add_handler(CommandHandler(COMMANDS, on_command))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    return app


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе каждый опрос Telegram в логе
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(",", " ").split()}
    if not allowed:
        log.warning("ALLOWED_USER_IDS пуст — бот никому не ответит, кроме сообщения с id")
    data_dir = os.environ.get("FINANCE_DATA_DIR", "data")
    os.makedirs(data_dir, exist_ok=True)
    storage = Storage(os.path.join(data_dir, "finance.db"))
    recognizer = ClaudeRecognizer(model=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL))
    flow = Flow(storage, recognizer, os.path.join(data_dir, "receipts"))
    build_app(token, flow, allowed).run_polling(allowed_updates=Update.ALL_TYPES)
