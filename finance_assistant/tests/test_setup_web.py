"""Страница настройки: id пользователей, ключ Claude API, токен бота."""
import json
import os
import stat
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

from finance import setup_web
from finance.setup_web import SetupServer, mask, parse_ids, read_env, write_env

ENV = """# Токен бота от @BotFather
TELEGRAM_BOT_TOKEN=111:OLDTOKEN
# Ключ Claude API
ANTHROPIC_API_KEY=sk-ant-old-key-1234
ALLOWED_USER_IDS=
FINANCE_DATA_DIR=/opt/finance-bot/data
TZ=Europe/Moscow
"""


@pytest.fixture
def server(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    saved = []
    srv = SetupServer(("127.0.0.1", 0), str(env), "SECRET123", checks=False,
                      on_saved=lambda: saved.append(1) or "перезапуск: ок")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv, base, env, saved, thread
    srv.shutdown()
    srv.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def post(url, data):
    req = urllib.request.Request(url, data=urlencode(data).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_env_roundtrip_keeps_comments_and_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    write_env(str(env), {"ALLOWED_USER_IDS": "123456789,987654321", "NEW_KEY": "x"})
    text = env.read_text(encoding="utf-8")
    assert "# Ключ Claude API" in text and "TZ=Europe/Moscow" in text
    assert read_env(str(env))["ALLOWED_USER_IDS"] == "123456789,987654321"
    assert read_env(str(env))["TELEGRAM_BOT_TOKEN"] == "111:OLDTOKEN"
    assert text.rstrip().endswith("NEW_KEY=x")
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o640


def test_parse_ids_and_mask():
    assert parse_ids("123456789, 987654321 123456789") == [123456789, 987654321]
    for bad in ("@anna", "12", "12345678901234567"):
        with pytest.raises(ValueError):
            parse_ids(bad)
    assert mask("sk-ant-api03-abcdef") == "•••cdef" and mask("") == ""


def test_secret_required(server):
    srv, base, *_ = server
    assert get(base + "/")[0] == 404
    assert get(base + "/WRONG")[0] == 404
    code, page = get(base + "/SECRET123")
    assert code == 200 and "Настройка финансового помощника" in page
    assert "sk-ant-old-key-1234" not in page and "•••1234" in page   # ключ целиком не показываем
    assert "OLDTOKEN" not in page


def test_post_needs_csrf(server):
    srv, base, env, *_ = server
    assert post(base + "/SECRET123", {"user1": "123456789"})[0] == 403
    assert read_env(str(env))["ALLOWED_USER_IDS"] == ""


def test_invalid_id_shows_error_and_keeps_env(server):
    srv, base, env, saved, _ = server
    code, page = post(base + "/SECRET123", {"csrf": srv.csrf, "user1": "@anna"})
    assert code == 200 and "не похоже на Telegram id" in page
    assert read_env(str(env))["ALLOWED_USER_IDS"] == "" and not saved


def test_save_updates_only_given_fields_and_closes(server):
    srv, base, env, saved, thread = server
    code, page = post(base + "/SECRET123", {
        "csrf": srv.csrf, "user1": "123456789", "user2": "987654321",
        "ANTHROPIC_API_KEY": "sk-ant-new-key-9999", "TELEGRAM_BOT_TOKEN": ""})
    assert code == 200 and "Сохранено" in page and "перезапуск: ок" in page
    values = read_env(str(env))
    assert values["ALLOWED_USER_IDS"] == "123456789,987654321"
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-new-key-9999"
    assert values["TELEGRAM_BOT_TOKEN"] == "111:OLDTOKEN"      # пустое поле — прежнее значение
    assert saved == [1]
    thread.join(timeout=5)
    assert not thread.is_alive()                                # страница закрылась


def test_whois_lists_people_who_wrote_to_bot(server, monkeypatch):
    srv, base, *_ = server
    monkeypatch.setattr(setup_web, "recent_senders",
                        lambda token: [{"id": 123456789, "name": "Анна (@anna)"}])
    code, body = post(base + "/SECRET123/whois", {"csrf": srv.csrf})
    assert code == 200 and json.loads(body)["users"][0]["id"] == 123456789


def test_too_many_wrong_attempts_close_page(server):
    srv, base, env, saved, thread = server
    for _ in range(setup_web.MAX_BAD_ATTEMPTS):
        get(base + "/guess")
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_telegram_token_format_checked_before_network():
    with pytest.raises(ValueError, match="123456789:ABC"):
        setup_web.check_telegram("no-colon-token")
