"""Официальные отчёты реализации WB по API (finance-api, категория токена «Финансы»).

Два метода из официальной спецификации (`13-finances.yaml`, тег «Финансовые
отчёты»):

- `POST /api/finance/v1/sales-reports/list` — список отчётов за период с
  итогами шапки, в том числе «Итого к оплате» (`bankPaymentSum`) — то самое
  число из ЛК WB, с которым сверяется итог строк;
- `POST /api/finance/v1/sales-reports/detailed/{reportId}` — детализация
  отчёта, частями по `rrdId` до ответа 204.

Старый `statistics-api …/reportDetailByPeriod` WB отключил летом 2026 года.

Лимит у обоих методов — ОДИН запрос в минуту на профиль продавца, поэтому
между вызовами клиент сам ждёт минуту, а 429 отрабатывает общий `with_retry`
(он понимает WB-шный `X-Ratelimit-Retry`). Из-за лимита загрузка идёт в
задании планировщика, а не в обработчике страницы.

На живом токене не проверялось: методы и поля — по официальной спецификации
(октябрь 2026). Первый прогон надо сверить с ЛК WB — для этого на странице
отчёта стоит «Итого к оплате» WB рядом с итогом строк.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Callable

import requests

from finapp.http_retry import with_retry

BASE_URL = "https://finance-api.wildberries.ru"
LIST_PATH = "/api/finance/v1/sales-reports/list"
DETAIL_PATH = "/api/finance/v1/sales-reports/detailed/{report_id}"
# Лимит WB — 1 запрос в минуту; секунда сверху — запас на неточность часов.
PAUSE_SECONDS = 61
PAGE_LIMIT = 100_000
# Отчёты по API есть с этой даты (спецификация WB).
FIRST_AVAILABLE = date(2024, 1, 29)

REPORT_TYPES = {1: "основной", 2: "по выкупам"}


class WbFinanceError(RuntimeError):
    """Человекочитаемая причина отказа (неверный токен, нет категории «Финансы»)."""


class WbFinanceApi:
    def __init__(self, token: str, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, pause: float = PAUSE_SECONDS):
        self.session = session or requests.Session()
        # Токен WB передаётся в Authorization как есть, без «Bearer»
        # (securitySchemes.HeaderApiKey спецификации).
        self.session.headers.update({"Authorization": token})
        self._sleep = sleep
        self._pause = pause
        self._calls = 0

    def _post(self, path: str, body: dict):
        # Пауза ПЕРЕД каждым запросом, кроме первого: лимит на профиль, а не на
        # метод, — список и детализация делят одну минуту.
        if self._calls:
            self._sleep(self._pause)
        self._calls += 1

        def call():
            r = self.session.post(f"{BASE_URL}{path}", json=body, timeout=120)
            if r.status_code in (401, 403):
                raise WbFinanceError(_auth_message(r))
            r.raise_for_status()
            return r

        response = with_retry(call)
        if response.status_code == 204 or not response.content:
            return []
        data = response.json()
        return data if isinstance(data, list) else []

    def list_reports(self, date_from: date, date_to: date) -> list[dict]:
        """Еженедельные отчёты, период которых попадает в [date_from, date_to]."""
        reports, offset = [], 0
        while True:
            page = self._post(LIST_PATH, {"dateFrom": f"{date_from:%Y-%m-%d}",
                                          "dateTo": f"{date_to:%Y-%m-%d}",
                                          "period": "weekly", "limit": 1000, "offset": offset})
            reports += page
            if len(page) < 1000:
                return reports
            offset += len(page)

    def report_lines(self, report_id: int, on_page: Callable[[int], None] | None = None) -> list[dict]:
        """Все строки детализации отчёта. Страница короче лимита — последняя:
        лишний запрос ради ответа 204 стоил бы целую минуту."""
        lines, rrd_id = [], 0
        while True:
            page = self._post(DETAIL_PATH.format(report_id=int(report_id)),
                              {"limit": PAGE_LIMIT, "rrdId": rrd_id})
            lines += page
            if on_page:
                on_page(len(lines))
            if len(page) < PAGE_LIMIT or not page:
                return lines
            rrd_id = page[-1].get("rrdId") or 0
            if not rrd_id:
                return lines


def _auth_message(response) -> str:
    detail = ""
    try:
        detail = (response.json() or {}).get("detail", "")
    except ValueError:
        pass
    if response.status_code == 401:
        return "WB ответил 401 — токен недействителен."
    if "scope" in detail or "category" in detail:
        return ("WB ответил 403 — у токена нет категории «Финансы». Создайте токен с ней в ЛК WB "
                "(Настройки → Доступ к API) и впишите его на «API-ключах» в поле токена для отчётов.")
    if "base token" in detail:
        return "WB ответил 403 — базовый токен к финансовым отчётам не допускается, нужен персональный."
    return f"WB ответил 403 — доступ к финансовым отчётам запрещён ({detail or 'без пояснения'})."
