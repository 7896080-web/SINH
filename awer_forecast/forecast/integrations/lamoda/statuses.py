"""Статусы позиций заказа (`OrderItemResponseStatusEnum` из OpenAPI v2) и их смысл для спроса.

Открытый вопрос №4 ТЗ — какие статусы считать продажей, а какие возвратом. Принятое
допущение Фазы 1 (сверено со списком значений спецификации, не с живыми данными):
- продажа — `DELIVERED` (выкуплено покупателем);
- возврат после выкупа — статусы группы REFUND_* (кроме REFUND_INVESTIGATION — это ещё
  разбирательство, не возврат);
- `NOT_BOUGHT`, `NOT_DELIVERED`, `CANCELED` — не продажа и не возврат (товар не выкупили).

Новые значения Lamoda добавляет без анонса — незнакомый статус не ломает приём:
строка сохраняется как есть, в спрос не идёт, в журнал пишется предупреждение.
"""
from __future__ import annotations

KNOWN_ITEM_STATUSES = frozenset({
    "SENT_BACK_TO_PARTNER", "LEFT_LM_EXPRESS", "ARRIVED_TO_LM_EXPRESS", "ON_SHELF", "CONFIRMED", "SHIPPED",
    "REFUND_INVESTIGATION", "READY_TO_REFUND", "DELIVERED", "GIVEN_TO_DELIVERY", "IN_DELIVERY", "REFUNDED",
    "REFUND_NECESSARY", "REFUND_BY_PARTNER", "ARRIVED_TO_WH", "SHIPPED_TO_WH", "AWAITING_SHIPMENT",
    "NOT_BOUGHT", "NOT_DELIVERED", "CANCELED", "CLAIMED_DEFECTIVE", "CLAIMED_OK", "CLAIMED_USED",
})

SALE_STATUSES = frozenset({"DELIVERED"})
RETURN_STATUSES = frozenset({"REFUND_NECESSARY", "READY_TO_REFUND", "REFUNDED", "REFUND_BY_PARTNER"})

# Статусы поставки FBO (`FBOShipmentStatusEnum`). Завершённые — товар уже на остатке FBO
# (или поставки не будет); всё остальное считаем «в пути на FBO». Незнакомый статус
# тоже считаем «в пути»: лишний раз не предложить отгрузку безопаснее, чем отправить дубль.
SHIPMENT_FINAL_STATUSES = frozenset({"CLOSED", "RECEIVED", "CANCELLED", "CANCEL_PENDING", "REJECTED"})
SHIPMENT_ARRIVED_STATUSES = frozenset({"CLOSED", "RECEIVED"})
# Отгрузку на FBO в 1С списывают с ЦС ПОСЛЕ создания поставки в Lamoda (ответ владельца, 21.10.2026).
# Пока поставка в этих статусах (до прибытия на склад Lamoda), её товар ещё числится в остатке
# ЦС из 1С: для страницы FBO он уже занят, а в закупке не должен считаться дважды
# (и в остатке ЦС, и «в пути»). Граница — допущение, настраивается PLAN_FBO_STATUSES_ON_CS.
SHIPMENT_ON_CS_STATUSES = frozenset({"DRAFT", "NEW", "SENT", "VALIDATION", "VALIDATION_ERROR", "CONFIRMED",
                                     "READY_FOR_SHIPMENT", "CHANGING_WAREHOUSE"})
KNOWN_SHIPMENT_STATUSES = frozenset({
    "NEW", "SENT", "CONFIRMED", "DRAFT", "CANCELLED", "CHANGING_WAREHOUSE", "VALIDATION", "VALIDATION_ERROR",
    "READY_FOR_SHIPMENT", "UNLOADING_NEEDED", "UNLOADING", "UNLOADED", "PROCESSING", "CLOSED", "REJECTED",
    "RECEIVED", "CANCEL_PENDING",
})


def normalize_status(value) -> str:
    return str(value or "").strip().upper()
