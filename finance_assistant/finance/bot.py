"""Telegram-обвязка: принимает апдейты, зовёт Flow, отправляет ответы.

Запуск: python -m finance (настройки — переменные окружения, см. README).
"""

import asyncio
import io
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .flow import Flow, Reply
from .recognize import ClaudeRecognizer, DEFAULT_MODEL
from .storage import Storage

log = logging.getLogger(__name__)

COMMANDS = ["start", "help", "cancel", "cards", "addcard", "delcard", "cats", "addcat",
            "list", "sverka", "done", "itog", "biz", "notbiz", "vypiska", "fix", "svod",
            "rules", "delrule"]
BATCH_WAIT = 2.5  # сек тишины после последнего файла — пачка собрана
MAX_TEXT = 4000  # лимит Telegram — 4096 единиц UTF-16 на сообщение
MAX_FILE = 20 * 1024 * 1024  # больше бот скачать не может
MAX_IMAGE = 5 * 1024 * 1024  # больше Claude API не примет одну картинку
# Что умеем разбирать, присланное файлом (не фото).
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
DOC_MIMES = {"application/pdf", "text/csv", "text/plain",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _units(text: str) -> int:
    """Длина так, как её считает Telegram: в единицах UTF-16 (эмодзи — две)."""
    return len(text.encode("utf-16-le")) // 2


def _split(text: str) -> list[str]:
    parts, chunk = [], ""
    for line in text.split("\n"):
        while _units(line) > MAX_TEXT:  # одна очень длинная строка — режем её саму
            cut = MAX_TEXT
            while _units(line[:cut]) > MAX_TEXT:
                cut -= 100
            if chunk:
                parts.append(chunk)
                chunk = ""
            parts.append(line[:cut])
            line = line[cut:]
        if _units(chunk) + _units(line) + 1 > MAX_TEXT and chunk:
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
        chat = update.effective_chat
        if not chat or chat.type != ChatType.PRIVATE:
            # В группе ответы с суммами и Excel увидели бы все участники —
            # работаем только в личном чате и в группах молчим.
            return False
        user = update.effective_user
        if user and user.id in allowed:
            return True
        # Бот видит финансовые данные — чужим не отвечаем ничем, кроме id,
        # чтобы владелец мог добавить себя в ALLOWED_USER_IDS при настройке.
        if update.effective_chat and update.message:
            await update.effective_chat.send_message(
                f"Доступ закрыт. Ваш Telegram id: {user.id if user else '?'}")
        return False

    async def keep_typing(chat):
        # «Печатает…» гаснет через ~5 секунд — продлеваем, пока идёт разбор.
        while True:
            try:
                await chat.send_action(ChatAction.TYPING)
            except Exception:  # сеть моргнула — не повод падать
                pass
            await asyncio.sleep(4)

    # Все обращения к Flow — строго по одному: у него одно соединение SQLite
    # и состояние чата, которое читается и пишется целиком.
    lock = asyncio.Lock()
    # Файлы, которые ещё собираются в пачку: chat_id → {items, update, timer}.
    pending: dict[int, dict] = {}

    async def run(update: Update, fn, *args, note: str | None = None):
        chat = update.effective_chat
        async with lock:
            if note:
                await chat.send_message(note)
            typing = asyncio.create_task(keep_typing(chat))
            try:
                # Распознавание — синхронный сетевой вызов на десятки секунд;
                # в отдельном потоке, чтобы не блокировать цикл событий.
                replies = await asyncio.to_thread(fn, chat.id, *args)
            finally:
                typing.cancel()
            await send(update, replies)

    def batch_note(n: int) -> str:
        if n == 1:
            return ("⏳ Разбираю… Скриншот — до минуты, выписка — до нескольких минут. "
                    "Присылать повторно не нужно.")
        minutes = max(1, round(n / 4 * 25 / 60))  # по 4 файла одновременно, ~25 с на файл
        return (f"⏳ Принял {n} файлов, разбираю — примерно {minutes} мин. "
                "Присылать повторно не нужно; по итогу пришлю сводку.")

    async def process_batch(chat_id: int):
        """Разобрать собранную пачку (или ничего, если её нет)."""
        batch = pending.pop(chat_id, None)  # сразу, без await: новая пачка начнётся заново
        if not batch:
            return
        timer = batch["timer"]
        if timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
        items = batch["items"]
        try:
            await run(batch["update"], flow.on_batch, items, note=batch_note(len(items)))
        except Exception:  # задача вне обработчика PTB — ошибку ловим сами
            log.exception("Ошибка при разборе пачки")
            await batch["update"].effective_chat.send_message(
                "Что-то пошло не так при разборе файлов, ничего из пачки не записано. "
                "Пришлите их ещё раз; если повторяется — /cancel.")

    async def batch_timer(chat_id: int):
        await asyncio.sleep(BATCH_WAIT)
        await process_batch(chat_id)

    async def add_to_batch(update: Update, item: dict):
        """Альбом приходит отдельными сообщениями — ждём тишины и разбираем разом."""
        chat_id = update.effective_chat.id
        batch = pending.setdefault(chat_id, {"items": []})
        batch["items"].append(item)
        batch["update"] = update
        if batch.get("timer"):
            batch["timer"].cancel()
        batch["timer"] = asyncio.create_task(batch_timer(chat_id))

    async def flush_first(update: Update):
        """Команда или кнопка после файлов: сначала разбираем присланное до неё."""
        await process_batch(update.effective_chat.id)

    async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        await flush_first(update)
        command = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
        await run(update, flow.on_command, command, " ".join(context.args or []))

    async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        photo = update.message.photo[-1]  # самый крупный вариант
        tg_file = await photo.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        await add_to_batch(update, dict(files=[(data, "image/jpeg")],
                                        caption=update.message.caption or "",
                                        filename="", file_ids=[photo.file_unique_id]))

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
        elif name.endswith(".pdf"):
            mime = "application/pdf"
        if mime not in IMAGE_MIMES | DOC_MIMES:
            hint = ("Фото в формате HEIC пришлите обычным фото (не файлом)."
                    if "heic" in mime or name.endswith((".heic", ".heif"))
                    else "Старый .xls пересохраните в .xlsx или пришлите PDF."
                    if name.endswith(".xls") else
                    "Принимаю фото и скриншоты, PDF, Excel (.xlsx) и CSV.")
            await update.effective_chat.send_message(f"Такой файл не разберу. {hint}")
            return
        if mime in IMAGE_MIMES and doc.file_size and doc.file_size > MAX_IMAGE:
            await update.effective_chat.send_message(
                "Картинка больше 5 МБ — пришлите её обычным фото (не файлом), "
                "Telegram сам её уменьшит.")
            return
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        await add_to_batch(update, dict(files=[(data, mime)], caption=update.message.caption or "",
                                        filename=name, file_ids=[doc.file_unique_id]))

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        await flush_first(update)
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
        await flush_first(update)
        await run(update, flow.on_button, query.data)

    async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
        log.exception("Ошибка при обработке апдейта", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            await update.effective_chat.send_message(
                "Что-то пошло не так, запись не сохранена. Попробуйте ещё раз; "
                "если ошибка повторяется — /cancel сбросит текущий вопрос.")

    # Только новые сообщения: отредактированное сообщение не должно
    # записываться второй раз (и у него нет update.message).
    new = filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler(COMMANDS, on_command, filters=new))
    app.add_handler(MessageHandler(new & filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(new & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, on_text))
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
    build_app(token, flow, allowed).run_polling(
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
