"""Постоянная страница настроек под паролем."""
import asyncio
import http.client
import os
import re
import threading
from urllib.parse import urlencode

import pytest

from finance import settings_web, setup_web
from finance.bot import watch_env
from finance.settings_web import (Guard, SettingsServer, check_new_password, hash_password,
                                  verify_password)
from finance.setup_web import read_env, write_env

PASSWORD = "Правильный-пароль-42"
ENV = "TELEGRAM_BOT_TOKEN=111:OLDTOKEN\nANTHROPIC_API_KEY=sk-ant-old-key-1234\nALLOWED_USER_IDS=\nTZ=Europe/Moscow\n"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_web.time, "sleep", lambda s: None)
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    clock = Clock()
    srv = SettingsServer(("127.0.0.1", 0), str(env), hash_password(PASSWORD, n=2 ** 12),
                         checks=False, guard=Guard(clock), secure_cookie=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, env, clock
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, data=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=urlencode(data or {}) if data is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode()
    return resp.status, dict(resp.getheaders()), body


def login(srv, password=PASSWORD):
    code, headers, body = request(srv, "POST", "/login", {"password": password})
    cookie = headers.get("Set-Cookie", "").split(";")[0]
    return code, cookie, body


def csrf_of(page):
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_password_hashing():
    stored = hash_password(PASSWORD, n=2 ** 12)
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)
    assert not verify_password("не тот пароль", stored)
    assert not verify_password(PASSWORD, "мусор")
    assert check_new_password("короткий") and check_new_password("1234567890")
    assert check_new_password("aaaaaaaaaaaa")
    assert check_new_password(PASSWORD) is None


def test_requires_login(site):
    srv, *_ = site
    code, headers, _ = request(srv, "GET", "/")
    assert code == 303 and headers["Location"] == "/login"
    assert request(srv, "POST", "/", {"ANTHROPIC_API_KEY": "x"})[0] == 303
    code, _, page = request(srv, "GET", "/login")
    assert code == 200 and 'type="password"' in page


def test_login_and_page(site):
    srv, env, _ = site
    code, cookie, _ = login(srv)
    assert code == 303 and cookie.startswith("fb_session=")
    code, headers, page = request(srv, "GET", "/", cookie=cookie)
    assert code == 200 and "Выйти" in page
    assert "sk-ant-old-key-1234" not in page and "•••1234" in page and "OLDTOKEN" not in page
    nonce = re.search(r"script-src 'nonce-([^']+)'", headers["Content-Security-Policy"]).group(1)
    assert f'<script nonce="{nonce}">' in page
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_wrong_password_and_ip_lock(site):
    srv, _, clock = site
    for _ in range(settings_web.IP_FAILS - 1):
        assert login(srv, "неверно")[0] == 401
    assert login(srv, "неверно")[0] == 401          # пятая — блокировка
    code, cookie, body = login(srv)                   # даже верный пароль не пускает
    assert code == 429 and "15 минут" in body and not cookie
    clock.now += settings_web.IP_LOCK + 1
    assert login(srv)[0] == 303


def test_global_lock():
    clock = Clock()
    g = Guard(clock)
    for i in range(settings_web.GLOBAL_FAILS):
        g.failed(f"10.0.0.{i}")                      # с разных адресов
    assert "на час" in g.blocked("10.9.9.9")
    clock.now += settings_web.GLOBAL_WINDOW + 1
    assert g.blocked("10.9.9.9") is None


def test_save_keeps_page_open_and_csrf(site):
    srv, env, _ = site
    _, cookie, _ = login(srv)
    page = request(srv, "GET", "/", cookie=cookie)[2]
    assert request(srv, "POST", "/", {"user1": "123456789"}, cookie)[0] == 403
    code, _, page = request(srv, "POST", "/", {
        "csrf": csrf_of(page), "user1": "123456789", "user2": "987654321",
        "ANTHROPIC_API_KEY": "sk-ant-new-key-9999"}, cookie)
    assert code == 200 and "Сохранено" in page
    values = read_env(str(env))
    assert values["ALLOWED_USER_IDS"] == "123456789,987654321"
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-new-key-9999"
    assert values["TELEGRAM_BOT_TOKEN"] == "111:OLDTOKEN" and values["TZ"] == "Europe/Moscow"
    assert request(srv, "GET", "/", cookie=cookie)[0] == 200   # страница работает дальше
    code, _, page = request(srv, "POST", "/", {"csrf": csrf_of(page), "user1": "@anna"}, cookie)
    assert "не похоже на Telegram id" in page


def test_logout_and_session_expiry(site):
    srv, _, clock = site
    _, cookie, _ = login(srv)
    page = request(srv, "GET", "/", cookie=cookie)[2]
    code, headers, _ = request(srv, "POST", "/logout", {"csrf": csrf_of(page)}, cookie)
    assert code == 303 and "Max-Age=0" in headers["Set-Cookie"]
    assert request(srv, "GET", "/", cookie=cookie)[0] == 303
    _, cookie, _ = login(srv)
    clock.now += settings_web.IDLE + 1
    assert request(srv, "GET", "/", cookie=cookie)[0] == 303     # 30 минут без действий


def test_forged_cookie_rejected(site):
    srv, *_ = site
    assert request(srv, "GET", "/", cookie="fb_session=forged-token-value")[0] == 303


def test_env_written_in_place_when_folder_not_writable(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    os.chmod(env, 0o640)
    inode = os.stat(env).st_ino
    monkeypatch.setattr(setup_web.os, "access", lambda path, mode: False)
    write_env(str(env), {"ALLOWED_USER_IDS": "123456789"})
    assert os.stat(env).st_ino == inode                          # тот же файл, права те же
    assert read_env(str(env))["ALLOWED_USER_IDS"] == "123456789"
    assert read_env(str(env))["TZ"] == "Europe/Moscow"


def test_bot_restarts_when_env_changes(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV)
    stopped = []
    app = type("App", (), {"stop_running": lambda self: stopped.append(1)})()

    async def scenario():
        task = asyncio.create_task(watch_env(app, str(env), interval=0.05))
        await asyncio.sleep(0.12)
        assert not stopped                                         # файл не менялся
        write_env(str(env), {"ALLOWED_USER_IDS": "123456789"})
        await asyncio.wait_for(task, 1)
    asyncio.run(scenario())
    assert stopped == [1]
