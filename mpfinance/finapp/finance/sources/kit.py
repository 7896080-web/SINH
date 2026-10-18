"""Отчёт о продажах Яндекс KIT за период — из официальных данных заказов.

Финансового отчёта у KIT нет ни в кабинете, ни в API (своей комиссии KIT не
берёт; комиссия эквайринга — у Яндекс Пэй, у неё отчёты только файлами, их
загружают на странице «Отчёты площадок»). Поэтому «официальный отчёт» KIT —
это сами заказы из `GET /v1/orders` (OpenAPI KIT):

- ПРОДАНО — позиция доставленной части заказа (`delivery_info.delivered_at`
  попадает в период, статус DELIVERED / COMPLETED / PARTIAL_REFUND /
  CREATING_FINAL_RECEIPTS / FROZEN): количество `quantity − refused_count`,
  выручка `final_price × количество` (цена за единицу после всех скидок);
- ОТКАЗ при получении (`refused_count`, весь заказ при FULL_REFUND) — не
  продажа и не возврат: товар до покупателя не дошёл. Деньги по нему не
  считаются, количество отказов пишется в итог загрузки;
- ДОСТАВКА — `seller_costs` (стоимость доставки для магазина) расходом,
  `customer_costs` (оплата доставки покупателем) доходом, по каждой части
  заказа, которую везли.

Возвраты ПОСЛЕ получения KIT не видит вовсе (они оформляются в Яндекс Пэй или
кассой) — их, как и комиссии, даёт отчёт Яндекс Пэй.

Номер заказа строки — `id:часть:позиция`, ровно как его записывает опрос
заказов (`platform_clients/kit.py`), поэтому сверка с принятыми заказами
работает без перевода номеров.

На живом токене этот разбор не проверялся. Проверено на живом API другими
частями системы: авторизация Bearer, постраничный `GET /v1/orders`
(`total_count`), `GET /v1/variants/{id}` с полем `barcode`. Что сверить на
первом отчёте: учитывает ли KIT `updated_from` (если нет — просто пройдём всю
историю, результат тот же) и сумму `final_price × количество` с
`purchased_price` части заказа.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Callable

import requests

from finapp.finance import values as v
from finapp.timeutils import local_date_of
from finapp.http_retry import with_retry

BASE_URL = "https://api.kit.yandex.net"
# Документированный лимит KIT — 3 запроса в секунду на магазин.
THROTTLE_SECONDS = 0.34
PER_PAGE = 100
MAX_PAGES = 500

SOLD_STATUSES = {"DELIVERED", "COMPLETED", "PARTIAL_REFUND", "CREATING_FINAL_RECEIPTS", "FROZEN"}
FULL_REFUSAL_STATUS = "FULL_REFUND"


class KitOrdersApi:
    def __init__(self, token: str, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self._sleep = sleep

    def _get(self, path: str, params=None):
        def call():
            self._sleep(THROTTLE_SECONDS)
            r = self.session.get(f"{BASE_URL}{path}", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def orders_updated_since(self, moment: datetime) -> list[dict]:
        """Заказы, изменённые не раньше `moment` (UTC). Доставка меняет заказ,
        поэтому все доставленные в периоде среди них. Если KIT фильтр не
        поддержит, вернётся вся история — отбор по дате доставки всё равно
        делается у нас."""
        orders, seen = [], set()
        stamp = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for page in range(1, MAX_PAGES + 1):
            data = self._get("/v1/orders", params={
                "page": page, "per_page": PER_PAGE, "sort_by": "updated_at",
                "sort_direction": "asc", "updated_from": stamp})
            batch = data.get("orders") or []
            for order in batch:
                # Заказ, изменившийся во время обхода, может сдвинуться между
                # страницами и прийти дважды.
                if order.get("id") not in seen:
                    seen.add(order.get("id"))
                    orders.append(order)
            total = data.get("total_count")
            if not batch or (isinstance(total, int) and page * PER_PAGE >= total):
                return orders
        raise RuntimeError(f"KIT: больше {MAX_PAGES} страниц заказов — обход остановлен, "
                           f"сузьте период")

    def variant_barcode(self, variant_id: str) -> str | None:
        try:
            data = self._get(f"/v1/variants/{variant_id}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None          # вариант удалён из каталога
            raise
        return (data.get("barcode") or "").strip() or None


def _parse_moment(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def build_lines(orders: list[dict], date_from: date, date_to: date,
                barcode_for: Callable[[str], str | None]) -> tuple[list[dict], dict]:
    """Строки единой формы за [date_from, date_to] и счётчики для итога загрузки."""
    lines: list[dict] = []
    stats = {"orders": 0, "sold_qty": 0, "refused_qty": 0, "no_barcode": 0}
    for order in orders:
        status = str(order.get("status") or "")
        if status not in SOLD_STATUSES and status != FULL_REFUSAL_STATUS:
            continue
        created = _parse_moment(order.get("created_at"))
        updated = _parse_moment(order.get("updated_at"))
        counted = False
        for chunk in order.get("delivery_chunks") or []:
            info = chunk.get("delivery_info") or {}
            delivered = _parse_moment(info.get("delivered_at"))
            # Полный отказ: до покупателя не доехало ничего, даты доставки может
            # не быть — расход на доставку относим к дате последнего изменения.
            moment = delivered or (updated if status == FULL_REFUSAL_STATUS else None)
            if moment is None:
                continue
            day = local_date_of(moment)
            if not (date_from <= day <= date_to):
                continue
            counted = True
            for item in chunk.get("items") or []:
                quantity = int(item.get("quantity") or 0)
                refused = quantity if status == FULL_REFUSAL_STATUS else int(item.get("refused_count") or 0)
                sold = max(quantity - refused, 0)
                stats["refused_qty"] += min(refused, quantity)
                if not sold:
                    continue
                variant_id = str(item.get("product_variant_id") or "")
                barcode = barcode_for(variant_id) if variant_id else None
                if not barcode:
                    stats["no_barcode"] += 1
                price = v.parse_money(item.get("final_price")) or 0
                line = v.empty_line()
                line.update(
                    operation=v.SALE, source_operation=f"Доставлен ({status})",
                    operation_date=day, order_date=local_date_of(created) if created else None,
                    order_id=f"{order.get('id')}:{chunk.get('id')}:{item.get('id')}",
                    srid=str(order.get("order_number") or ""), barcode=barcode or "",
                    platform_sku=variant_id, name=str(item.get("name") or "")[:255],
                    quantity=sold, revenue=price * sold, payout=price * sold,
                )
                lines.append(line)
                stats["sold_qty"] += sold
            seller = v.parse_money(info.get("seller_costs")) or 0
            customer = v.parse_money(info.get("customer_costs")) or 0
            if seller or customer:
                line = v.empty_line()
                line.update(
                    operation=v.LOGISTICS, source_operation="Доставка KIT",
                    operation_date=day, order_id=f"{order.get('id')}:{chunk.get('id')}",
                    srid=str(order.get("order_number") or ""),
                    logistics=seller, compensation=customer,
                )
                lines.append(line)
        if counted:
            stats["orders"] += 1
    return lines, stats
