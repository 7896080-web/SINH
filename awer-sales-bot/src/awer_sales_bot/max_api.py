"""Минимальный клиент MAX Bot API (https://dev.max.ru/docs-api), сверен 18.10.2026 — скилл .claude/skills/max-bot-api.

Адрес https://platform-api2.max.ru (с 19.07.2026); токен — заголовок `Authorization: <token>`, без Bearer
(query `access_token` больше не поддерживается). Сертификат сервера выдан Минцифры (Russian Trusted Root CA) —
его нет в стандартном наборе Python, поэтому он лежит в certs/ и добавляется к системным и certifi.
Методы: GET /me, PATCH /me/commands, POST /messages?user_id=…, POST /answers?callback_id=…, GET /updates.

Повторы: 429 и сетевая ошибка до отправки запроса — всегда; 5xx и обрыв после отправки — только для GET:
POST /messages не идемпотентен, повтор даст продавцу то же сообщение дважды.
"""
from __future__ import annotations

import ssl
import time
from pathlib import Path

import certifi
import httpx

BASE_URL = "https://platform-api2.max.ru"
CA_FILE = Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem"  # корневой сертификат Минцифры


class MaxError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, code: str = ""):
        super().__init__(msg)
        self.status, self.code = status, code


class MaxAuthError(MaxError):
    """401 — токен не принят (отозван, опечатка в .env): повторять бессмысленно, нужен новый токен."""


class MaxDenied(MaxError):
    """403 (chat.denied) — пользователь остановил бота или заблокировал его: это не сбой."""


def ssl_context(extra_ca: str | Path | None = None) -> ssl.SSLContext:
    """Системные корневые сертификаты (в Windows — хранилище) + certifi + сертификат Минцифры (+ свой файл)."""
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(certifi.where())
    for f in (CA_FILE, extra_ca):
        if f and Path(f).is_file():
            ctx.load_verify_locations(str(f))
    return ctx


class MaxClient:
    def __init__(self, token: str, base_url: str = BASE_URL, ca_file: str | Path | None = None,
                 timeout: float = 40.0, pause: float = 1.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.pause = pause  # первая пауза перед повтором, дальше удваивается
        self.http = httpx.Client(timeout=timeout, verify=ssl_context(ca_file))

    def _req(self, method: str, path: str, params: dict | None = None, json: dict | None = None,
             timeout: float | None = None) -> dict:
        safe = method == "GET"  # GET можно повторять при любом сбое, POST — только если запрос не ушёл
        err: object = None
        for attempt in range(4):
            if attempt:
                time.sleep(self.pause * 2 ** (attempt - 1))
            try:
                r = self.http.request(method, self.base + path, params=params, json=json,
                                      headers={"Authorization": self.token}, timeout=timeout or self.timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
                err = e  # соединения не было — сервер запрос не получил
                continue
            except httpx.TransportError as e:
                if not safe:
                    raise MaxError(f"{method} {path}: обрыв связи, отправка не подтверждена ({e})") from e
                err = e
                continue
            if r.status_code == 429 or (r.status_code >= 500 and safe):
                err = f"{r.status_code} {r.text[:200]}"
                continue
            if r.status_code >= 400:
                raise self._error(method, path, r)
            return r.json() if r.content else {}
        raise MaxError(f"{method} {path}: сервер недоступен ({err})")

    @staticmethod
    def _error(method: str, path: str, r: httpx.Response) -> MaxError:
        try:
            body = r.json()
        except ValueError:
            body = {}
        code = str(body.get("code") or "") if isinstance(body, dict) else ""
        msg = f"{method} {path}: {r.status_code} {code} {r.text[:300]}".strip()
        if r.status_code == 401:
            return MaxAuthError(msg, 401, code)
        if r.status_code == 403 and code == "chat.denied":  # продавец остановил бота; другой 403 — сбой (аудит Р9)
            return MaxDenied(msg, 403, code)
        return MaxError(msg, r.status_code, code)

    def me(self) -> dict:
        return self._req("GET", "/me")

    def set_commands(self, commands: list[tuple[str, str]]) -> dict:
        """Меню команд бота (PATCH /me/commands): до 32 команд, имя до 64 символов, описание до 128."""
        return self._req("PATCH", "/me/commands",
                         json={"commands": [{"name": n[:64], "description": d[:128]} for n, d in commands[:32]]})

    def send(self, text: str, user_id: int | None = None, chat_id: int | None = None, fmt: str = "markdown",
             buttons: list[list[dict]] | None = None) -> dict:
        params = {"user_id": user_id} if user_id else {"chat_id": chat_id}
        body: dict = {"text": text[:4000], "format": fmt}
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        return self._req("POST", "/messages", params=params, json=body)

    def answer(self, callback_id: str, notification: str | None = None, message: dict | None = None) -> dict:
        """Ответ на нажатие кнопки (POST /answers): короткое уведомление и/или замена сообщения с кнопками."""
        body: dict = {}
        if notification:
            body["notification"] = notification
        if message:
            body["message"] = message
        return self._req("POST", "/answers", params={"callback_id": callback_id}, json=body)

    def updates(self, marker: int | None = None, timeout: int = 30) -> dict:
        params: dict = {"timeout": timeout, "types": "message_created,bot_started,bot_stopped,message_callback"}
        if marker is not None:
            params["marker"] = marker
        # HTTP-таймаут больше времени ожидания событий на сервере, иначе каждый пустой опрос — ошибка
        return self._req("GET", "/updates", params=params, timeout=max(self.timeout, timeout + 15))
