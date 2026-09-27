"""Telegram-обвязка: принимает апдейты, зовёт Flow, отправляет ответы.

Запуск: python -m finance — боевой бот, python -m finance --test — тестовый
(настройки — переменные окружения, см. README).
"""

import argparse
import asyncio
import io
import logging
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .clock import local_today
from .flow import Reply
from .mode import run_config
from .recognize import ClaudeRecognizer, DEFAULT_MODEL
from .users import UserSpaces, parse_user_ids

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


def _marked(text: str, label: str) -> str:
    return f"{label}\n{text}" if label and text else text


async def send(update: Update, replies: list[Reply], label: str = ""):
    """label — пометка тестового режима перед каждым ответом («🧪 ТЕСТ»)."""
    chat = update.effective_chat
    for r in replies:
        chunks = _split(_marked(r.text, label)) or [""]
        for i, chunk in enumerate(chunks):
            markup = None
            if r.buttons and i == len(chunks) - 1:
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                     for row in r.buttons])
            await chat.send_message(chunk, reply_markup=markup)
        if r.file:
            name, data = r.file
            if label:
                name = "ТЕСТ-" + name
            await chat.send_document(InputFile(io.BytesIO(data), filename=name))


ENV_CHECK_INTERVAL = 10  # сек: как часто смотреть, не поменялись ли настройки


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


async def watch_env(app: Application, path: str, interval: float = ENV_CHECK_INTERVAL):
    """Настройки поменяли на странице настроек — завершаемся, systemd запустит
    бота заново уже с новым .env (Restart=always)."""
    known = _mtime(path)
    while True:
        await asyncio.sleep(interval)
        current = _mtime(path)
        if current != known:
            log.warning("Файл настроек %s изменился — перезапускаюсь с новыми настройками", path)
            app.stop_running()
            return


TEST_HELP = ("🧪 Это ТЕСТОВЫЙ бот. Всё, что вы здесь записываете, хранится отдельно "
             "и в боевой учёт не попадает. Пробуйте смело: скриншоты, выписки, сверку, "
             "правила.\n/reset — стереть все тестовые данные и начать с чистого листа.")
RESET_YES, RESET_NO = "reset:yes", "reset:no"


def build_app(token: str, flow_for, allowed: set[int], env_file: str | None = None,
              label: str = "", reset=None) -> Application:
    """flow_for(user_id) → Flow этого пользователя (у каждого своя база).

    Можно передать и один Flow (в тестах) — тогда он общий. env_file — следить
    за файлом настроек и перезапускаться при его изменении.
    Тестовый режим: label — пометка перед каждым ответом, reset(user_id) —
    стереть данные пользователя (команда /reset есть только тогда).
    """
    builder = Application.builder().token(token)
    if env_file:
        async def start_watch(application: Application):
            application.create_task(watch_env(application, env_file))
        builder = builder.post_init(start_watch)
    app = builder.build()
    resolve = flow_for if callable(flow_for) else (lambda user_id: flow_for)

    async def say(chat, text: str, **kwargs):
        await chat.send_message(_marked(text, label), **kwargs)

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
            await say(update.effective_chat, 
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
    # Своя очередь у каждого пользователя: у него своя база, а разбор пачки
    # у одного не должен задерживать другого.
    locks: dict[int, asyncio.Lock] = {}
    # Файлы, которые ещё собираются в пачку: chat_id → {items, update, timer}.
    pending: dict[int, dict] = {}

    async def run(update: Update, method: str, *args, note: str | None = None):
        """Вызвать метод Flow того, кто прислал сообщение или нажал кнопку.

        Данные выбираются только по Telegram id отправителя — чужие недоступны
        даже через кнопку из пересланного сообщения.
        """
        chat = update.effective_chat
        user_id = update.effective_user.id
        fn = getattr(resolve(user_id), method)
        async with locks.setdefault(user_id, asyncio.Lock()):
            if note:
                await say(chat, note)
            typing = asyncio.create_task(keep_typing(chat))
            try:
                # Распознавание — синхронный сетевой вызов на десятки секунд;
                # в отдельном потоке, чтобы не блокировать цикл событий.
                replies = await asyncio.to_thread(fn, chat.id, *args)
            finally:
                typing.cancel()
            await send(update, replies, label)

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
            await run(batch["update"], "on_batch", items, note=batch_note(len(items)))
        except Exception:  # задача вне обработчика PTB — ошибку ловим сами
            log.exception("Ошибка при разборе пачки")
            await say(batch["update"].effective_chat, 
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
        if command == "reset":
            await ask_reset(update)
            return
        await run(update, "on_command", command, " ".join(context.args or []))
        if label and command in ("start", "help"):
            await say(update.effective_chat, TEST_HELP)

    async def ask_reset(update: Update):
        if reset is None:  # в боевом режиме команды нет вовсе
            await update.effective_chat.send_message("Не знаю такой команды. /help — что я умею.")
            return
        await send(update, [Reply(
            "Стереть ВСЕ тестовые данные: карты, записи, выписки, правила, скриншоты? "
            "Боевой учёт это не затронет.",
            [[("🗑 Да, стереть", RESET_YES), ("Нет", RESET_NO)]])], label)

    async def do_reset(update: Update, data: str):
        chat = update.effective_chat
        if data == RESET_NO or reset is None:
            await say(chat, "Ничего не стёр.")
            return
        user_id = update.effective_user.id
        async with locks.setdefault(user_id, asyncio.Lock()):
            await asyncio.to_thread(reset, user_id)
        await say(chat, "🗑 Тестовые данные стёрты. Начинаем с чистого листа: /addcard …")

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
            await say(update.effective_chat, "Файл больше 20 МБ — Telegram не даст его скачать. "
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
            await say(update.effective_chat, f"Такой файл не разберу. {hint}")
            return
        if mime in IMAGE_MIMES and doc.file_size and doc.file_size > MAX_IMAGE:
            await say(update.effective_chat, 
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
        await run(update, "on_text", update.message.text)

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
        if query.data in (RESET_YES, RESET_NO):
            await do_reset(update, query.data)
            return
        await run(update, "on_button", query.data)

    async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
        log.exception("Ошибка при обработке апдейта", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            await say(update.effective_chat, 
                "Что-то пошло не так, запись не сохранена. Попробуйте ещё раз; "
                "если ошибка повторяется — /cancel сбросит текущий вопрос.")

    # Только новые сообщения: отредактированное сообщение не должно
    # записываться второй раз (и у него нет update.message).
    new = filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler(COMMANDS + (["reset"] if reset else []), on_command,
                                   filters=new))
    app.add_handler(MessageHandler(new & filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(new & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    return app


def wait_for_settings(env_file: str | None, interval: float | None = None):
    """Бот ещё не настроен (нет токена) или настроен с ошибкой: ждём, пока
    поменяют .env (например, на странице настроек), и выходим — systemd
    запустит заново уже с новыми настройками. Без .env ждать нечего."""
    if not env_file:
        raise SystemExit("Бот не настроен — см. ошибку выше")
    known = _mtime(env_file)
    while _mtime(env_file) == known:
        time.sleep(interval or ENV_CHECK_INTERVAL)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m finance")
    parser.add_argument("--test", action="store_true",
                        help="тестовый режим: отдельный бот и отдельные данные")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе каждый опрос Telegram в логе
    env_file = os.environ.get("FINANCE_ENV_FILE")
    try:
        config = run_config(os.environ, test=args.test)
    except ValueError as exc:
        # Не бьёмся в перезапусках: ждём, пока настройки поправят.
        log.error("%s бот не запущен: %s — жду правки настроек",
                  "Тестовый" if args.test else "Боевой", exc)
        wait_for_settings(env_file)
        return
    if config.is_test and not config.token:
        log.info("Тестовый бот не настроен (нет TELEGRAM_BOT_TOKEN_TEST) — жду настроек")
        wait_for_settings(env_file)
        return
    if not config.user_ids:
        log.warning("ALLOWED_USER_IDS пуст — бот никому не ответит, кроме сообщения с id")
    os.makedirs(config.data_dir, mode=0o700, exist_ok=True)
    recognizer = ClaudeRecognizer(model=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL))
    spaces = UserSpaces(config.data_dir, recognizer, config.user_ids, allow_reset=config.is_test,
                        today=local_today)
    if not config.is_test:
        spaces.migrate_shared_data()
    log.info("Режим: %s. Пользователей: %d, у каждого своя база в %s/users/<id>/",
             "ТЕСТОВЫЙ" if config.is_test else "боевой", len(config.user_ids), config.data_dir)
    build_app(config.token, spaces.flow, set(config.user_ids), env_file=env_file,
              label=config.label, reset=spaces.reset if config.is_test else None).run_polling(
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
