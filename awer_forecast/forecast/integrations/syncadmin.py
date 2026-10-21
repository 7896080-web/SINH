"""Спрос WB, Ozon и сайта — из журнала перемещений, которые Sync Admin проводит в 1С.

Решение владельца (21.10.2026): спрос этих каналов берём из 1С, а не из API площадок. В
выгрузке для «бота план продавцов» их нет; в 1С они видны только перемещениями, которые
создаёт Sync Admin: на каждый заказ — `CREATE_MOVEMENT` (ЦС → «<Площадка>.Ожидает»), на
отмену или частичный возврат — `CANCEL_MOVEMENT` с количеством возврата. Те же строки
лежат в базе Sync Admin (таблица `ftp_tasks`, ответ 1С — в `result_status`/`result_detail`).
Читаем их оттуда, ничего не меняя в обработке 1С.

Правила (см. app/workers/order_poller.py Sync Admin):
- `is_test` — симуляция со страницы «Тестирование»: никогда не спрос;
- у заказа одна товарная строка (`processed_orders` уникален по кабинету и заказу), одно
  `CREATE_MOVEMENT` и не больше одного `CANCEL_MOVEMENT`; при повторной отправке берём
  последнее задание создания;
- спрос заказа = количество − отменённое/возвращённое, на день заказа (`movement_date`,
  если перемещение проведено задним числом, иначе день создания задания по Москве);
- статус ответа 1С не фильтруем: заказ случился на площадке, даже если 1С не провела
  документ, — для прогноза важен спрос, а не бухгалтерия.

База Sync Admin открывается ТОЛЬКО НА ЧТЕНИЕ (SQLite `mode=ro`); запись в неё — путь к
оверселлу и расхождению с 1С, см. корневой CLAUDE.md.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Integer, String, bindparam, create_engine, text
from sqlalchemy.engine import Engine

from forecast.timeutil import msk_date

PLATFORM_CHANNELS = {"wb": "wb", "ozon": "ozon", "kit": "own_site"}
CHANNELS = frozenset(PLATFORM_CHANNELS.values())
MAX_LISTED = 100

TASKS_SQL = text("""
    SELECT t.id, t.command, t.barcode, t.quantity, t.order_id, t.account_id, t.movement_date,
           t.created_at, a.platform
    FROM ftp_tasks t JOIN platform_accounts a ON a.id = t.account_id
    WHERE t.is_test = :is_test
      AND t.command IN ('CREATE_MOVEMENT', 'CANCEL_MOVEMENT')
      AND t.created_at >= :since
    ORDER BY t.id
""").bindparams(bindparam("is_test", type_=Boolean), bindparam("since", type_=DateTime)).columns(
    id=Integer, command=String, barcode=String, quantity=Integer, order_id=String, account_id=Integer,
    movement_date=Date, created_at=DateTime, platform=String,
)


def open_sync_admin(source: str) -> Engine:
    """`source` — путь к sync_admin.db (откроется только на чтение) или URL PostgreSQL
    (тогда роль в URL должна иметь только право чтения)."""
    if "://" in source:
        return create_engine(source, pool_pre_ping=True)
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"нет базы Sync Admin: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True, timeout=30,
                                                                      check_same_thread=False))


@dataclass
class MarketplaceDemand:
    totals: dict[tuple[str, str, date], int] = field(default_factory=dict)   # (канал, баркод, день) → шт.
    orders: int = 0
    cancelled: int = 0
    partially_returned: int = 0
    skipped: list[str] = field(default_factory=list)


def _platform(value) -> str:
    text_value = str(value or "").strip()
    return text_value.split(".")[-1].lower()            # «Platform.wb» на случай, если enum записан именем


def aggregate(rows, since_day: date) -> MarketplaceDemand:
    creates: dict[tuple[int, str], object] = {}
    cancels: dict[tuple[int, str], int] = defaultdict(int)
    for row in rows:
        key = (row.account_id, row.order_id)
        if row.command == "CREATE_MOVEMENT":
            creates[key] = row                           # повторная отправка — берём последнее
        else:
            cancels[key] += int(row.quantity or 0)
    result = MarketplaceDemand()
    totals: dict[tuple[str, str, date], int] = defaultdict(int)
    for key, row in creates.items():
        channel = PLATFORM_CHANNELS.get(_platform(row.platform))
        if channel is None:
            if len(result.skipped) < MAX_LISTED:
                result.skipped.append(f"заказ {row.order_id}: неизвестная площадка «{row.platform}»")
            continue
        if not row.barcode:
            if len(result.skipped) < MAX_LISTED:
                result.skipped.append(f"заказ {row.order_id} ({channel}): без баркода")
            continue
        day = row.movement_date or msk_date(row.created_at)
        if day < since_day:
            continue
        result.orders += 1
        quantity = int(row.quantity or 0)
        returned = cancels.get(key, 0)
        if returned >= quantity:
            result.cancelled += 1
            continue
        if returned:
            result.partially_returned += 1
        totals[(channel, row.barcode.strip(), day)] += quantity - returned
    result.totals = dict(totals)
    return result


def read_marketplace_demand(engine: Engine, since_day: date) -> MarketplaceDemand:
    """Заказы с дня `since_day` (по Москве). Задания берём с запасом в сутки: день заказа —
    московский, а время создания задания в Sync Admin — UTC."""
    since = datetime.combine(since_day - timedelta(days=1), datetime.min.time())
    with engine.connect() as conn:
        rows = conn.execute(TASKS_SQL, {"is_test": False, "since": since}).all()
    return aggregate(rows, since_day)
