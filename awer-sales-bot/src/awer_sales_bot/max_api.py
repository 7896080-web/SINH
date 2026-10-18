"""Минимальный клиент MAX Bot API (https://dev.max.ru/docs-api).

ПРОВЕРИТЬ ПО ДОКУМЕНТАЦИИ перед запуском (API молодой, менялся):
  * способ передачи токена: заголовок `Authorization: <token>` (config max.auth=header)
    или query-параметр `access_token` (max.auth=query);
  * формат апдейтов (`bot_started`, `message_created`) — поля разобраны в bot.py.
Методы: GET /me, POST /messages?user_id=…, GET /updates (long polling).
"""
from __future__ import annotations

import time

import httpx


class MaxError(RuntimeError):
    pass


class MaxClient:
    def __init__(self, token: str, base_url: str = "https://platform-api.max.ru", auth: str = "header",
                 timeout: float = 40.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.auth = auth
        self.http = httpx.Client(timeout=timeout)

    def _req(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict:
        params = dict(params or {})
        headers = {}
        if self.auth == "query":
            params["access_token"] = self.token
        else:
            headers["Authorization"] = self.token
        for attempt in range(4):
            r = self.http.request(method, self.base + path, params=params, json=json, headers=headers)
            if r.status_code == 429 or r.status_code >= 500:  # лимит/сбой — экспоненциальная пауза
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise MaxError(f"{method} {path}: {r.status_code} {r.text[:300]}")
            return r.json() if r.content else {}
        raise MaxError(f"{method} {path}: сервер недоступен")

    def me(self) -> dict:
        return self._req("GET", "/me")

    def send(self, text: str, user_id: int | None = None, chat_id: int | None = None, fmt: str = "markdown",
             buttons: list[list[dict]] | None = None) -> dict:
        params = {"user_id": user_id} if user_id else {"chat_id": chat_id}
        body: dict = {"text": text[:4000], "format": fmt}
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        return self._req("POST", "/messages", params=params, json=body)

    def updates(self, marker: int | None = None, timeout: int = 30) -> dict:
        params: dict = {"timeout": timeout, "types": "message_created,bot_started"}
        if marker is not None:
            params["marker"] = marker
        return self._req("GET", "/updates", params=params)
