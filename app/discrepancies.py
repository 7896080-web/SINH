"""Товары, у которых учёт 1С разошёлся со складом.

`Product.stock_discrepancy` — главное число всего механизма порога
(`порог = расхождение + бронь`), и до сих пор увидеть его можно было только
построчно в каталоге на 152 тысячи позиций либо скриптом с консоли сервера.
То есть вопрос «а где у нас вообще расхождения и откуда они взялись» ответа не
имел, хотя ответ система записывает: `StockDiscrepancyLog` помнит, чем и когда
поставлено каждое число.

**Знак значим, и стороны неравноценны.** Плюс — «в 1С числится больше, чем
лежит»: мы придерживаем ровно столько и недопродаём, если измерение устарело.
Минус — «на складе больше, чем знает 1С»: наружу ОСОЗНАННО уходит больше
учётного остатка, и устаревшее измерение здесь уже оверселл. Поэтому минус
показывается отдельным отбором, а не тонет в общем списке.

**Ноль сюда не попадает намеренно.** Ноль значит «измеряли, склад сошёлся» — это
не расхождение, а его отсутствие, и держать такие строки в списке «с
расхождениями» значит утопить в них то, ради чего список открыли. NULL («не
измеряли») не попадает тем более.

Список рабочий, а не отчётный: с ним идут пересчитывать склад. Поэтому он живёт
отдельной страницей, а не находкой отчёта — ненулевое расхождение это НОРМА, и
находка, срабатывающая на норме, приучает пролистывать отчёт целиком.
"""

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DiscrepancySource, Product, StockDiscrepancyLog
from app.transmit import explain

# Сколько строк показываем. Список рабочий: осмысленная порция важнее полноты,
# полную даёт выгрузка. Тот же потолок, что у «Товаров» (`FILTERED_LIMIT`), и по
# той же причине — строка весит килобайты, а браузер их раскладывает.
PAGE_LIMIT = 500

# Потолок выгрузки: столько строк собрать в .xlsx не жалко.
EXPORT_LIMIT = 20000

SOURCE_LABELS = {
    DiscrepancySource.fact: "введён факт",
    DiscrepancySource.manual: "правка руками",
    DiscrepancySource.offset: "задан порог",
    DiscrepancySource.reset: "сброс порога",
    DiscrepancySource.migration: "перенос при обновлении",
}


@dataclass
class DiscrepancyRow:
    uid_1c: str
    article: str
    name: str
    size: str
    color: str
    stock: int
    reserve: int
    discrepancy: int
    offset: int | None
    outgoing: int            # сколько уходит на площадки прямо сейчас
    broadcasting: bool
    # Происхождение числа — из `StockDiscrepancyLog`. Без него строка говорит
    # «расхождение −2» и молчит о том, кто и на каком основании это решил, а
    # пересчитывать склад идут именно с этим вопросом.
    source: str
    username: str
    measured_at: object      # datetime | None
    base_date: object        # date | None
    base_stock: int | None
    fact: int | None


def _origins(db: Session, uids: list[str]) -> dict:
    """Последняя запись истории по каждому товару — ОДНИМ запросом на страницу.

    Не по строке: на каталоге в 152 тысячи позиций подзапрос на товар и есть тот
    самый анти-паттерн, который уже ронял «Товары» и сверку остатков.
    """
    if not uids:
        return {}
    newest = (db.query(func.max(StockDiscrepancyLog.id))
              .filter(StockDiscrepancyLog.uid_1c.in_(uids))
              .group_by(StockDiscrepancyLog.uid_1c).scalar_subquery())
    return {row.uid_1c: row for row in
            db.query(StockDiscrepancyLog).filter(
                StockDiscrepancyLog.id.in_(newest)).all()}


def collect(db: Session, *, only_negative: bool = False,
            only_broadcasting: bool = False,
            limit: int = PAGE_LIMIT) -> tuple[list[DiscrepancyRow], int]:
    """(строки, сколько всего подходит). Второе число нужно, чтобы честно
    написать «показано N из M», а не делать вид, что это весь список."""
    query = db.query(Product).filter(
        Product.stock_discrepancy.isnot(None),
        Product.stock_discrepancy != 0,
    )
    if only_negative:
        query = query.filter(Product.stock_discrepancy < 0)
    if only_broadcasting:
        query = query.filter(Product.broadcast_enabled.is_(True))

    total = query.order_by(None).enable_eagerloads(False).count()
    # По МОДУЛЮ убывания: разбирают такой список сверху, а величина расхождения и
    # есть мера того, насколько учёт разошёлся со складом. Сортируй мы по знаку,
    # первыми шли бы самые отрицательные — но плюс на 200 штук стоит разбора не
    # меньше, чем минус на 3.
    products = (query.order_by(func.abs(Product.stock_discrepancy).desc(),
                               Product.article)
                .limit(limit).all())

    origins = _origins(db, [p.uid_1c for p in products])
    rows = []
    for p in products:
        log = origins.get(p.uid_1c)
        rows.append(DiscrepancyRow(
            uid_1c=p.uid_1c, article=p.article or "", name=p.name or "",
            size=p.size or "", color=p.color or "",
            stock=p.stock_on_hand or 0, reserve=p.reserve or 0,
            discrepancy=p.stock_discrepancy, offset=p.broadcast_offset,
            # Тем же путём, что колонка «Уходит» на «Товарах»: разойдись они,
            # одна страница обещала бы не то, что другая.
            outgoing=explain(p, None, None).quantity,
            broadcasting=bool(p.broadcast_enabled),
            source=SOURCE_LABELS.get(log.source, "") if log else "",
            username=(log.username or "") if log else "",
            measured_at=log.created_at if log else None,
            base_date=log.base_date if log else None,
            base_stock=log.base_stock if log else None,
            fact=log.fact if log else None,
        ))
    return rows, total
