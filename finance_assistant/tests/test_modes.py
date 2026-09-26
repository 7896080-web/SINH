"""Боевой и тестовый режимы: тестовый бот не может задеть боевые данные."""
import asyncio
import configparser
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.constants import ChatType

from finance import bot
from finance.bot import RESET_NO, RESET_YES, TEST_HELP, build_app
from finance.flow import Reply
from finance.mode import TEST_MARK, ConfigError, run_config, same_bot
from finance.users import UserSpaces

from conftest import TODAY, FakeRecognizer, payment, png
from test_settings_web import csrf_of, login, request, site  # noqa: F401 — фикстура

PROD_TOKEN = "111:PROD-SECRET"
TEST_TOKEN = "222:TEST-SECRET"
ANNA, BORIS = 1001, 1002
DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


# --- настройки режима -------------------------------------------------------

def test_prod_config():
    cfg = run_config({"TELEGRAM_BOT_TOKEN": PROD_TOKEN, "TELEGRAM_BOT_TOKEN_TEST": TEST_TOKEN,
                      "FINANCE_DATA_DIR": "/srv/data", "ALLOWED_USER_IDS": "1,2"})
    assert (cfg.mode, cfg.token, cfg.data_dir, cfg.user_ids) == ("prod", PROD_TOKEN, "/srv/data", [1, 2])
    assert not cfg.is_test and cfg.label == ""
    with pytest.raises(ConfigError):
        run_config({})


def test_test_config_uses_own_token_and_folder():
    env = {"TELEGRAM_BOT_TOKEN": PROD_TOKEN, "TELEGRAM_BOT_TOKEN_TEST": TEST_TOKEN,
           "FINANCE_DATA_DIR": "/srv/data/", "ALLOWED_USER_IDS": "1,2"}
    cfg = run_config(env, test=True)
    assert (cfg.token, cfg.data_dir, cfg.user_ids) == (TEST_TOKEN, "/srv/data-test", [1, 2])
    assert cfg.is_test and cfg.label == TEST_MARK
    assert run_config({**env, "TEST_USER_IDS": "1"}, test=True).user_ids == [1]
    # Без токена — не ошибка: тестовый бот просто не настроен.
    assert run_config({"TELEGRAM_BOT_TOKEN": PROD_TOKEN}, test=True).token == ""


def test_test_bot_must_be_another_bot():
    # Перевыпущенный токен того же бота: секрет другой, id бота тот же.
    for test_token in (PROD_TOKEN, "111:REISSUED"):
        with pytest.raises(ConfigError, match="тот же бот"):
            run_config({"TELEGRAM_BOT_TOKEN": PROD_TOKEN, "TELEGRAM_BOT_TOKEN_TEST": test_token},
                       test=True)
    assert same_bot("5:a", "5:b") and not same_bot("5:a", "6:a") and not same_bot("", "")


@pytest.mark.parametrize("test_dir", ["{p}", "{p}/", "{p}/users", "{p}/../data", "{parent}"])
def test_test_folder_must_not_overlap_prod(tmp_path, test_dir):
    prod = tmp_path / "data"
    prod.mkdir()
    env = {"TELEGRAM_BOT_TOKEN": PROD_TOKEN, "TELEGRAM_BOT_TOKEN_TEST": TEST_TOKEN,
           "FINANCE_DATA_DIR": str(prod),
           "FINANCE_TEST_DATA_DIR": test_dir.format(p=prod, parent=tmp_path)}
    with pytest.raises(ConfigError, match="пересекается"):
        run_config(env, test=True)


# --- данные: отдельные папки, сброс только в тесте --------------------------

def _spaces(folder, **kw):
    return UserSpaces(str(folder), FakeRecognizer(), [ANNA, BORIS], today=lambda: TODAY, **kw)


def _record(spaces, user):
    flow = spaces.flow(user)
    flow.db.add_card("Сбер", "1111", "Сбер")
    flow.recognizer.payments.append(payment())
    flow.on_files(user, [png()], "")
    return flow


def test_test_and_prod_data_are_separate(tmp_path):
    prod = _spaces(tmp_path / "data")
    test = _spaces(tmp_path / "data-test", allow_reset=True)
    _record(test, ANNA)
    assert len(test.flow(ANNA).db.expenses_between("2026-01-01", "2026-12-31")) == 1
    assert prod.flow(ANNA).db.expenses_between("2026-01-01", "2026-12-31") == []


def test_reset_only_in_test_mode_and_only_own_data(tmp_path):
    prod = _spaces(tmp_path / "data")
    _record(prod, ANNA)
    with pytest.raises(PermissionError):
        prod.reset(ANNA)
    assert prod.flow(ANNA).db.expenses_between("2026-01-01", "2026-12-31")

    test = _spaces(tmp_path / "data-test", allow_reset=True)
    _record(test, ANNA)
    _record(test, BORIS)
    test.reset(ANNA)
    assert not os.path.exists(test.user_dir(ANNA))
    fresh = test.flow(ANNA)                             # чистый лист, база снова создаётся
    assert fresh.db.cards() == [] and fresh.db.categories()
    assert test.flow(BORIS).db.expenses_between("2026-01-01", "2026-12-31")
    assert prod.flow(ANNA).db.expenses_between("2026-01-01", "2026-12-31")
    with pytest.raises(PermissionError):
        test.reset(999)


# --- Telegram: пометка, /reset ---------------------------------------------

class Flow:
    def __init__(self):
        self.calls = []

    def on_command(self, chat_id, command, arg):
        self.calls.append(command)
        return [Reply("Отчёт", file=("svod.xlsx", b"x"))]

    def on_button(self, chat_id, data):
        self.calls.append(data)
        return [Reply("ok")]


def _update(user, text="/help", data=None):
    sent = []

    async def send_message(text, **kw):
        sent.append(("text", text, kw.get("reply_markup")))

    async def send_document(doc, **kw):
        sent.append(("file", doc.filename, None))

    async def noop(*a, **kw):
        return None
    chat = SimpleNamespace(id=user, type=ChatType.PRIVATE, send_message=send_message,
                           send_action=noop, send_document=send_document)
    query = SimpleNamespace(data=data, answer=noop, edit_message_reply_markup=noop)
    message = SimpleNamespace(text=text)
    return SimpleNamespace(effective_chat=chat, effective_user=SimpleNamespace(id=user),
                           message=message, callback_query=query), sent


def _handlers(app):
    return {type(h).__name__: h for h in app.handlers[0]}


def test_test_bot_marks_every_reply_and_file():
    flow = Flow()
    h = _handlers(build_app(TEST_TOKEN, flow, {ANNA}, label=TEST_MARK, reset=lambda u: None))
    upd, sent = _update(ANNA, "/svod")
    asyncio.run(h["CommandHandler"].callback(upd, SimpleNamespace(args=[])))
    assert sent[0][1].startswith(TEST_MARK + "\n") and sent[1] == ("file", "ТЕСТ-svod.xlsx", None)
    stranger, sent = _update(999)
    asyncio.run(h["CommandHandler"].callback(stranger, SimpleNamespace(args=[])))
    assert sent[0][1].startswith(TEST_MARK) and "Доступ закрыт" in sent[0][1]


def test_help_in_test_mode_explains_it():
    h = _handlers(build_app(TEST_TOKEN, Flow(), {ANNA}, label=TEST_MARK, reset=lambda u: None))
    upd, sent = _update(ANNA, "/help")
    asyncio.run(h["CommandHandler"].callback(upd, SimpleNamespace(args=[])))
    assert TEST_HELP in sent[-1][1]


def test_prod_bot_unmarked_and_has_no_reset():
    flow = Flow()
    app = build_app(PROD_TOKEN, flow, {ANNA})
    h = _handlers(app)
    assert "reset" not in h["CommandHandler"].commands
    upd, sent = _update(ANNA, "/svod")
    asyncio.run(h["CommandHandler"].callback(upd, SimpleNamespace(args=[])))
    assert sent[0][1] == "Отчёт" and sent[1][1] == "svod.xlsx"
    # Кнопка «стереть» из пересланного тестового сообщения боевой бот не исполнит.
    upd, sent = _update(ANNA, data=RESET_YES)
    asyncio.run(h["CallbackQueryHandler"].callback(upd, None))
    assert "Ничего не стёр" in sent[-1][1]


def test_reset_asks_confirmation_then_wipes():
    wiped = []
    flow = Flow()
    h = _handlers(build_app(TEST_TOKEN, flow, {ANNA}, label=TEST_MARK, reset=wiped.append))
    assert "reset" in h["CommandHandler"].commands
    upd, sent = _update(ANNA, "/reset")
    asyncio.run(h["CommandHandler"].callback(upd, SimpleNamespace(args=[])))
    assert wiped == [] and flow.calls == []
    buttons = [b.callback_data for row in sent[0][2].inline_keyboard for b in row]
    assert buttons == [RESET_YES, RESET_NO]
    upd, sent = _update(ANNA, data=RESET_NO)
    asyncio.run(h["CallbackQueryHandler"].callback(upd, None))
    assert wiped == []
    upd, sent = _update(ANNA, data=RESET_YES)
    asyncio.run(h["CallbackQueryHandler"].callback(upd, None))
    assert wiped == [ANNA] and "стёрты" in sent[-1][1]
    assert flow.calls == []                              # до Flow кнопка не дошла


# --- запуск -----------------------------------------------------------------

def test_unconfigured_test_bot_waits_for_settings(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=111:A\n")
    monkeypatch.setattr(bot, "ENV_CHECK_INTERVAL", 0.01)
    for key in ("TELEGRAM_BOT_TOKEN_TEST", "FINANCE_TEST_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:A")
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FINANCE_ENV_FILE", str(env_file))
    monkeypatch.setattr(bot, "build_app", lambda *a, **k: pytest.fail("не должен запускаться"))
    done = threading.Event()

    def run():
        bot.main(["--test"])
        done.set()
    threading.Thread(target=run, daemon=True).start()
    assert not done.wait(0.2)                            # ждёт
    os.utime(env_file, ns=(0, 10 ** 18))                 # настройки поменяли
    assert done.wait(2)                                  # выходит — systemd перезапустит
    assert not (tmp_path / "data-test").exists()


def test_misconfigured_test_bot_does_not_crash_loop(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PROD_TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TEST", PROD_TOKEN)
    monkeypatch.setenv("FINANCE_ENV_FILE", str(env_file))
    waited = []
    monkeypatch.setattr(bot, "wait_for_settings", waited.append)
    bot.main(["--test"])
    assert waited == [str(env_file)]
    with pytest.raises(SystemExit):                      # боевой с ошибкой — падает громко
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
        bot.main([])


def test_systemd_units_isolate_test_bot():
    def unit(name):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read(DEPLOY / name, encoding="utf-8")
        return cp["Service"]
    prod, test = unit("finance-bot.service"), unit("finance-bot-test.service")
    assert not prod["ExecStart"].endswith("--test") and test["ExecStart"].endswith(" --test")
    assert prod["ReadWritePaths"] == "/opt/finance-bot/data"
    assert test["ReadWritePaths"] == "/opt/finance-bot/data-test"
    assert test["InaccessiblePaths"] == "-/opt/finance-bot/data"
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "finance-bot-test.service" in install and '"$APP/data-test"' in install


# --- страница настроек ------------------------------------------------------

def _save(srv, cookie, **form):
    page = request(srv, "GET", "/", cookie=cookie)[2]
    return request(srv, "POST", "/", {"csrf": csrf_of(page), **form}, cookie)[2]


def test_settings_page_test_bot(site):  # noqa: F811
    srv, env, _ = site
    _, cookie, _ = login(srv)
    assert "Сейчас: выключен" in request(srv, "GET", "/", cookie=cookie)[2]
    page = _save(srv, cookie, TELEGRAM_BOT_TOKEN_TEST="111:OTHER")   # боевой бот — 111
    assert "отдельным ботом" in page and "TELEGRAM_BOT_TOKEN_TEST" not in env.read_text()
    page = _save(srv, cookie, TELEGRAM_BOT_TOKEN_TEST="333:TESTBOT")
    assert "Сохранено" in page and "TELEGRAM_BOT_TOKEN_TEST=333:TESTBOT" in env.read_text()
    assert "включён" in request(srv, "GET", "/", cookie=cookie)[2]
    # Сменить боевой токен на тестового бота нельзя.
    page = _save(srv, cookie, TELEGRAM_BOT_TOKEN="333:X")
    assert "отдельным ботом" in page and "TELEGRAM_BOT_TOKEN=111:OLDTOKEN" in env.read_text()
    page = _save(srv, cookie, test_off="1")
    assert "выключен" in page and "TELEGRAM_BOT_TOKEN_TEST=\n" in env.read_text()
