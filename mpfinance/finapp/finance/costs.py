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

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import func
from sqlalchemy.orm import Session

from finapp import settings
from finapp.finance import values as v
from finapp.models import Product, ProductCost, SalesReportLine
from finapp.timeutils import now_utc, today_local

# Размеры, которых на площадках нет (владелец, 19.10.2026): «U» — карточки 1С не
# для маркетплейсов. В списках «без себестоимости» и в счётчиках страницы они
# только мешали искать то, что действительно искажает прибыль.
OFF_MARKET_SIZES = ("U",)


def off_market(size_column):
    """Условие «размер не с площадок» (без учёта регистра и пробелов)."""
    return func.upper(func.trim(func.coalesce(size_column, ""))).in_(OFF_MARKET_SIZES)


def sold_without_cost(db: Session) -> int:
    """Сколько проданных на площадках товаров 1С без себестоимости — то, что
    показывает «без себестоимости» на странице и по чему суточное задание решает,
    не запросить ли себестоимость из 1С раньше срока."""
    sold = db.query(SalesReportLine.uid_1c).filter(
        SalesReportLine.operation == v.SALE, SalesReportLine.uid_1c.isnot(None))
    return db.query(func.count(Product.uid_1c)).filter(
        Product.uid_1c.in_(sold), ~off_market(Product.size),
        ~Product.uid_1c.in_(db.query(ProductCost.uid_1c))).scalar() or 0

_CHUNK = 500
# Цена — число с точкой (или запятой), без пробелов ВНУТРИ: пробел — признак
# того, что во втором поле не цена. 18.10 на бою справочник баркодов
# (`uid|артикул|наименование|баркод|размер|цвет`) загрузили как себестоимость,
# а разбор убирал пробелы «как разделители тысяч» и брал второе поле: артикул
# «70534 10146» стал ценой $7 053 410 146, и дашборд показал себестоимость WB
# 692 млрд ₽.
_PRICE_RE = re.compile(r"^\d+(?:[.,]\d{1,4})?$")
# Доля строк не по формату, при которой файл не принимается ЦЕЛИКОМ: это уже не
# пара испорченных строк, а другой файл. Половину чужого файла, разобранную
# «как получилось», не принимаем — она хуже, чем ничего.
MAX_BAD_SHARE = 0.05


class CostFileError(ValueError):
    pass


def parse_cost_file(content: str) -> tuple[dict[str, int], int]:
    """`uid|цена` → {uid: центы}; второе — число неразобранных строк.

    Строка — ровно два поля. Файл, в котором строк не по формату больше
    `MAX_BAD_SHARE` (и их больше одной) или не по формату все, —
    `CostFileError` с примером строки: угадывать, в каком
    поле чужого файла цена, нельзя (в справочнике баркодов последнее поле —
    ЦВЕТ, и у части товаров он числом)."""
    costs, bad, lines, example = {}, 0, 0, ""
    for line in content.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        lines += 1
        parts = line.split("|")
        price_text = parts[1].strip() if len(parts) == 2 else ""
        if len(parts) != 2 or not parts[0].strip() or not _PRICE_RE.match(price_text):
            bad += 1
            example = example or line
            continue
        price = Decimal(price_text.replace(",", "."))
        if price <= 0:
            bad += 1
            example = example or line
            continue
        costs[parts[0].strip()] = int((price * 100).to_integral_value())
    # Одна испорченная строка — счёт, а не отказ (даже в маленьком файле);
    # файл, где не по формату всё или больше `MAX_BAD_SHARE`, — чужой.
    if lines and bad > lines * MAX_BAD_SHARE and (bad > 1 or bad == lines):
        fields = example.count("|") + 1
        hint = (" — похоже на справочник баркодов (uid|артикул|наименование|баркод|размер|цвет): "
                "его загружают как «справочник баркодов»") if fields >= 5 else ""
        raise CostFileError(f"это не файл себестоимости: строк не в формате uid|цена — {bad} из {lines}, "
                            f"например «{example[:120]}» ({fields} полей){hint}")
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
    manual: set[str] = set()
    for start in range(0, len(uids), _CHUNK):
        chunk = uids[start:start + _CHUNK]
        known |= {uid for (uid,) in db.query(Product.uid_1c).filter(Product.uid_1c.in_(chunk))}
        for cost in db.query(ProductCost).filter(ProductCost.uid_1c.in_(chunk)) \
                .order_by(ProductCost.valid_from.asc()):
            # Ручная цена, действующая на сегодня, — приоритетнее 1С
            # (`economics.CostBook`), хоть она и старше по дате записи.
            if cost.source == "excel" and cost.valid_from <= today:
                manual.add(cost.uid_1c)
            elif cost.source != "excel":
                latest[cost.uid_1c] = cost
    for uid in uids:
        if uid not in known:
            stats["unknown"] += 1
            continue
        if uid in manual:
            stats["manual"] += 1
            continue
        current = latest.get(uid)
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
    # Сколько проданного осталось без цены ПОСЛЕ выгрузки: этих 1С не знает, и
    # переспрашивать о них раньше суток незачем (`jobs._costs_newly_missing`).
    settings.put(db, settings.COSTS_MISSING_AT_LOAD, str(sold_without_cost(db)))
    return stats


def summary(stats: dict, bad: int = 0) -> str:
    return (f"себестоимость: строк {stats['rows']}, обновлено {stats['updated']}, без изменений "
            f"{stats['same']}, задано вручную (не тронуто) {stats['manual']}, нет в справочнике "
            f"{stats['unknown']}" + (f", неразобранных строк {bad}" if bad else ""))
