"""AWER CRM — продажи сайта на KIT прямо из базы CRM. ТОЛЬКО ЧТЕНИЕ.

CRM стоит на том же сервере (`C:\\awer_crm`, данные — `C:\\awer_crm_data\\kitcrm.db`),
сама забирает заказы из KIT и строит выгрузку «Продажи» (`sales-items-…xlsx`).
Здесь — то же правило, восстановленное по её базе и сверенное с её выгрузками
за июль, август и сентябрь 2026 года до строки и до рубля:

- заказ в статусе `COMPLETED`;
- строка — одна выкупленная штука позиции доставки: `quantity − refused_count`
  (отказ при получении — не продажа);
- «Сумма» — `final_price` позиции (цена после всех скидок), до комиссии;
- дата — `delivered_at` доставки (момент вручения; у KIT — с поясом).

Расхождение с CRM одно, и оно намеренное. Заказу без даты вручения (KIT её не
прислал: доставка застыла в «передан в доставку», а заказ выполнен) CRM ставит
дату из истории статусов, а если нет и её — время СВОЕЙ последней записи
заказа (`orders.updated_at`). Последнее меняется при каждой пересинхронизации
CRM: 13 летних заказов (27 штук) лежали в сентябрьском отчёте от 30.09
(367 714 ₽), а в том же отчёте от 18.10 их уже нет (309 950 ₽) — переехали в
октябрь, и, загрузи оба файла, посчитались бы дважды. Здесь дата не меняется:
первый переход в COMPLETED по истории CRM, иначе время последнего изменения
заказа в САМОМ KIT (`raw_payload.updated_at`) — для выполненного заказа это и
есть время выполнения.

Открывается SQLite-соединением с `mode=ro` (как база sync_admin в `syncdb.py`):
записать в базу CRM невозможно на уровне соединения. Схему CRM не импортируем
— читаем простым SQL четыре таблицы: `orders`, `order_items`,
`order_status_history`, `product_cache`.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from finapp import config
from finapp.finance import values as v
from finapp.timeutils import local_date_of, local_day_start_utc

COMPLETED = "COMPLETED"
# Как датирована строка — для сообщения загрузки: оператор должен знать, у
# скольких продаж дата не из вручения.
DELIVERED, HISTORY, KIT = "delivered", "history", "kit"

# Колонки, без которых правило не собрать. Разойдётся схема (новая версия CRM)
# — честный отказ с именем колонки, а не тихо пустой отчёт.
REQUIRED = {
    "orders": ("id", "order_number", "status", "kit_created_at", "raw_payload"),
    "order_items": ("order_id", "product_variant_id", "product_sku", "product_barcode", "product_name"),
    "order_status_history": ("order_id", "new_status", "changed_at"),
    "product_cache": ("variant_id", "sku", "barcode"),
}


class CrmDbError(RuntimeError):
    pass


@dataclass
class CrmSale:
    order_number: str
    day: date
    barcode: str
    article: str
    name: str
    amount_kop: int
    dated_by: str


def path() -> Path | None:
    raw = (config.CRM_DB_PATH or "").strip()
    return Path(raw) if raw else None


def available() -> bool:
    p = path()
    return p is not None and p.exists()


def status_text() -> str:
    p = path()
    if p is None:
        return "не задана (MPFIN_CRM_DB) — продажи сайта только выгрузкой CRM файлом"
    if not p.exists():
        return f"файла {p} нет — продажи сайта только выгрузкой CRM файлом"
    return f"{p} (только чтение)"


@contextmanager
def _connect():
    p = path()
    if p is None:
        raise CrmDbError("база AWER CRM не задана (MPFIN_CRM_DB в .env)")
    if not p.exists():
        raise CrmDbError(f"нет файла базы AWER CRM {p}")
    uri = p.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as e:
        raise CrmDbError(f"база AWER CRM не открылась: {e}") from e
    try:
        yield conn
    except sqlite3.Error as e:
        raise CrmDbError(f"чтение базы AWER CRM: {e}") from e
    finally:
        conn.close()


def _check_schema(conn: sqlite3.Connection) -> None:
    for table, columns in REQUIRED.items():
        have = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if not have:
            raise CrmDbError(f"в базе AWER CRM нет таблицы {table} — это не kitcrm.db или схема CRM изменилась")
        missing = [c for c in columns if c not in have]
        if missing:
            raise CrmDbError(f"в базе AWER CRM у таблицы {table} нет колонок {', '.join(missing)} — "
                             f"схема CRM изменилась, правило отчёта надо сверить заново")


def _moment(value) -> datetime | None:
    """Момент из базы CRM. С поясом — как прислал KIT; без пояса — UTC
    (так SQLAlchemy CRM пишет свои отметки)."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _amount(value) -> int | None:
    try:
        return v.parse_money(value)
    except v.ValueParseError:
        return None


def sales_between(date_from: date, date_to: date) -> tuple[list[CrmSale], dict]:
    """Продажи сайта с МЕСТНОЙ датой продажи в [date_from, date_to]: по строке
    на штуку. Возвращает (продажи, счётчики для сообщения загрузки)."""
    stats = {"orders": 0, "pieces": 0, "refused": 0, HISTORY: 0, KIT: 0, "undated": 0,
             "unreadable": 0, "no_barcode": 0, "bad_price": 0}
    # Заказ, созданный после конца периода, продан в нём быть не может; нижней
    # границы нет — вручают и через недели после оформления.
    upper = local_day_start_utc(date_to + timedelta(days=1)).astimezone(timezone.utc) \
        .replace(tzinfo=None).isoformat(sep=" ")
    sales: list[CrmSale] = []
    with _connect() as conn:
        _check_schema(conn)
        items: dict[tuple[str, str], tuple[str, str, str]] = {}
        for oid, vid, sku, barcode, name in conn.execute(
                "SELECT i.order_id, i.product_variant_id, i.product_sku, i.product_barcode, i.product_name "
                "FROM order_items i JOIN orders o ON o.id = i.order_id WHERE o.status = ?", (COMPLETED,)):
            items[(oid, vid or "")] = (sku or "", barcode or "", name or "")
        cache = {vid: (sku or "", barcode or "") for vid, sku, barcode in conn.execute(
            "SELECT variant_id, sku, barcode FROM product_cache")}
        completed_at = dict(conn.execute(
            "SELECT order_id, MIN(changed_at) FROM order_status_history WHERE new_status = ? "
            "GROUP BY order_id", (COMPLETED,)))
        rows = conn.execute(
            "SELECT id, order_number, raw_payload FROM orders WHERE status = ? "
            "AND (kit_created_at IS NULL OR kit_created_at < ?)", (COMPLETED, upper)).fetchall()

    for oid, number, payload in rows:
        try:
            order = json.loads(payload) if isinstance(payload, str) else payload
            chunks = order.get("delivery_chunks") or []
        except (TypeError, ValueError, AttributeError):
            stats["unreadable"] += 1
            continue
        # Дата без вручения — одна на заказ: история CRM, иначе KIT.
        fallback, fallback_by = _moment(completed_at.get(oid)), HISTORY
        if fallback is None:
            fallback, fallback_by = _moment(order.get("updated_at")) or _moment(order.get("created_at")), KIT
        counted = False
        for chunk in chunks:
            info = chunk.get("delivery_info") or {}
            moment, dated_by = _moment(info.get("delivered_at")), DELIVERED
            if moment is None:
                moment, dated_by = fallback, fallback_by
            for item in chunk.get("items") or []:
                pieces = int(item.get("quantity") or 0) - int(item.get("refused_count") or 0)
                if moment is None:
                    stats["undated"] += max(pieces, 0)
                    continue
                day = local_date_of(moment)
                if not (date_from <= day <= date_to):
                    continue
                stats["refused"] += int(item.get("refused_count") or 0)
                if pieces <= 0:
                    continue
                amount = _amount(item.get("final_price"))
                if amount is None:
                    stats["bad_price"] += pieces
                    continue
                vid = item.get("product_variant_id") or ""
                sku, barcode, name = items.get((oid, vid), ("", "", ""))
                cached_sku, cached_barcode = cache.get(vid, ("", ""))
                barcode = barcode or cached_barcode
                if not barcode:
                    stats["no_barcode"] += pieces
                if dated_by != DELIVERED:
                    stats[dated_by] += pieces
                counted = True
                stats["pieces"] += pieces
                sale = CrmSale(order_number=str(number or ""), day=day, barcode=barcode,
                               article=sku or cached_sku, name=item.get("name") or name,
                               amount_kop=amount, dated_by=dated_by)
                sales.extend(sale for _ in range(pieces))
        stats["orders"] += 1 if counted else 0
    sales.sort(key=lambda s: (s.day, s.order_number, s.barcode))
    return sales, stats


def to_line(sale: CrmSale) -> dict:
    """Строка единой формы — та же, что даёт выгрузка CRM файлом
    (`formats.build_awer_crm`): номер для покупателя — в `srid`, а не в
    `order_id`, потому что Sync Admin принимает заказы KIT по своим номерам."""
    line = v.empty_line()
    line["operation"] = v.SALE
    line["source_operation"] = "Продажа (AWER CRM)"
    line["quantity"] = 1
    line["revenue"] = line["payout"] = sale.amount_kop
    line["operation_date"] = sale.day
    line["srid"] = sale.order_number
    line["barcode"] = sale.barcode
    line["article"] = sale.article
    line["name"] = sale.name
    return line
