"""Точка заказа и страховой запас (раздел 5.5 ТЗ), базовая формула.

    L               = срок поставки + интервал пересмотра (незащищённое окно)
    страховой_запас = z × σ_ошибки × √L
    точка_заказа    = спрос_в_день × L + страховой_запас

Пополняем «до уровня» точки заказа: к отгрузке/закупке = ⌈точка_заказа⌉ − имеющееся
(остаток + в пути), не меньше нуля. Округление уровня вверх — при любом ненулевом
спросе и пустой позиции система предложит хотя бы одну штуку.

Расширенная формула с разбросом срока поставщика и окна для прерывистого спроса —
Фаза 2 (нужны SBC-классификация и статистика поставщика).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Policy:
    daily_demand: float
    sigma: float
    lead_days: float
    z: float
    safety_stock: float
    reorder_point: float
    position: float
    need: int                 # целых штук до уровня точки заказа


def safety_stock(z: float, sigma: float, lead_days: float) -> float:
    if lead_days <= 0 or sigma <= 0:
        return 0.0
    return z * sigma * math.sqrt(lead_days)


def order_up_to(daily_demand: float, sigma: float, lead_days: float, z: float, position: float) -> Policy:
    ss = safety_stock(z, sigma, lead_days)
    rop = max(daily_demand, 0.0) * lead_days + ss
    target = math.ceil(rop - 1e-9) if rop > 1e-9 else 0
    need = max(0, target - math.floor(position + 1e-9))
    return Policy(daily_demand=daily_demand, sigma=sigma, lead_days=lead_days, z=z, safety_stock=ss,
                  reorder_point=rop, position=position, need=need)
