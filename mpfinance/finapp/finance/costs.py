"""Себестоимость из 1С — той же командой, что читает «Репрайсер».

Обработка «ОбменССайтом» умеет `EXPORT_COST_PRICES|ключ` (добавлена для
«Репрайсера», сверена в Конфигураторе 02.10.2026): читает срез последних регистра
«Себестоимость номенклатуры» и кладёт `cost_<метка>.txt` в папку ответов того
задания, которое её попросило, — РАНЬШЕ ответа `result_<метка>.txt`. Для
заданий этой программы это `results\\finance`.

Строка файла: `uid_1c|цена` — цена в ДОЛЛАРАХ с точкой (так её ведёт база). В
рубли она переводится в юнит-экономике курсом ЦБ на дату продажи (`rates.py`).
Разбор ответа — `onec.apply_result_text`.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from finapp import settings
from finapp.models import Product, ProductCost
from finapp.timeutils import now_utc, today_local

_CHUNK = 500


def parse_cost_file(content: str) -> tuple[dict[str, int], int]:
    """`uid|цена` → {uid: центы}; второе — число неразобранных строк."""
    costs, bad = {}, 0
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 2 or not parts[0].strip():
            bad += 1
            continue
        try:
            price = Decimal(parts[1].strip().replace(" ", "").replace(",", "."))
        except InvalidOperation:
            bad += 1
            continue
        if not price.is_finite() or price <= 0:
            bad += 1
            continue
        costs[parts[0].strip()] = int((price * 100).to_integral_value())
    return costs, bad


def import_costs(db: Session, costs: dict[str, int]) -> dict:
    """Записать себестоимость из 1С с сегодняшнего числа. Не коммитит.

    - неизменившаяся цена новой записи не порождает (иначе история цен росла бы
      строкой на товар в сутки);
    - товар, себестоимость которого последней задана ВРУЧНУЮ (Excel), не
      трогаем: ручная цена — осознанная поправка человека, и суточная выгрузка не
      вправе её молча перебить;
    - товара, которого нет в справочнике, пропускаем (его всё равно не продать)."""
    stats = {"rows": len(costs), "updated": 0, "same": 0, "manual": 0, "unknown": 0}
    today = today_local()
    uids = sorted(costs)
    known: set[str] = set()
    latest: dict[str, ProductCost] = {}
    for start in range(0, len(uids), _CHUNK):
        chunk = uids[start:start + _CHUNK]
        known |= {uid for (uid,) in db.query(Product.uid_1c).filter(Product.uid_1c.in_(chunk))}
        for cost in db.query(ProductCost).filter(ProductCost.uid_1c.in_(chunk)) \
                .order_by(ProductCost.valid_from.asc()):
            latest[cost.uid_1c] = cost
    for uid in uids:
        if uid not in known:
            stats["unknown"] += 1
            continue
        current = latest.get(uid)
        if current is not None and current.source == "excel":
            stats["manual"] += 1
            continue
        if current is not None and (current.currency, current.cost_minor) == ("USD", costs[uid]):
            stats["same"] += 1
            continue
        if current is not None and current.valid_from == today:
            current.cost_minor, current.currency, current.source = costs[uid], "USD", "1c"
        else:
            db.add(ProductCost(uid_1c=uid, valid_from=today, cost_minor=costs[uid],
                               currency="USD", source="1c", updated_by="1С"))
        stats["updated"] += 1
    db.flush()
    settings.put(db, settings.COSTS_LOADED_AT, now_utc().isoformat(timespec="seconds"))
    return stats


def summary(stats: dict, bad: int = 0) -> str:
    return (f"себестоимость: строк {stats['rows']}, обновлено {stats['updated']}, без изменений "
            f"{stats['same']}, задано вручную (не тронуто) {stats['manual']}, нет в справочнике "
            f"{stats['unknown']}" + (f", неразобранных строк {bad}" if bad else ""))
