"""Есть на складе — нет на площадке.

Товар лежит на ЦС, продавать его можно, а карточки на площадке нет — значит он
не продаётся нигде, и никто об этом не узнает: рассылка по такой паре молчит
(отправлять не по чему), заказов не будет, в глаза это не бросается. 20.09 на
бою так нашлось пятьдесят пар по Kit — их увидели только потому, что рассылка
наконец начала их называть.

Площадка, а не кабинет — вот главное решение этого модуля. У WB три кабинета
(разные ИП), и карточка заводится в ОДНОМ из них: товар, лежащий в кабинете
Яворской, с точки зрения продаж на WB присутствует. Считать его отсутствующим в
двух других значит выдать двести лишних строк на каждую сотню настоящих. У Ozon
и Kit кабинет один, и там площадка и кабинет — одно и то же.

Каталог берётся от ВСЕХ кабинетов площадки, включая выключенные: каталог
остаётся и от кабинета, который погасил предохранитель, а карточка на площадке
от этого никуда не делась.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Barcode, Platform, PlatformAccount, PlatformCatalogItem, Product

# Сколько строк отдаём в интерфейс. Список этот — рабочий: с ним идут заводить
# карточки, и осмысленная порция важнее полноты. Полную даёт выгрузка в Excel.
PAGE_LIMIT = 500

PLATFORM_LABELS = {
    Platform.wb: "Wildberries",
    Platform.ozon: "Ozon",
    Platform.kit: "Яндекс KIT",
}


@dataclass
class MissingRow:
    uid_1c: str
    article: str
    name: str
    size: str
    color: str
    stock: int
    # Площадки, где карточки нет: {Platform: True/False} — True значит «нет».
    missing: dict


def _uids_with_card(db: Session, platform: Platform) -> set[str]:
    """Товары 1С, у которых карточка на этой площадке есть.

    Связь идёт через баркод: он один и тот же у нас и в каталоге кабинета. У
    размер-цвета бывает несколько баркодов, и достаточно совпасть одному — это
    та же карточка.
    """
    rows = (
        db.query(Barcode.uid_1c)
        .join(PlatformCatalogItem, PlatformCatalogItem.barcode == Barcode.barcode)
        .join(PlatformAccount, PlatformAccount.id == PlatformCatalogItem.account_id)
        .filter(PlatformAccount.platform == platform)
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def platforms_in_use(db: Session) -> list[Platform]:
    """Площадки, по которым вообще есть кабинеты. Спрашивать про площадку, к
    которой не подключались, бессмысленно: там нет ни одной карточки, и весь
    склад окажется «отсутствующим»."""
    rows = db.query(PlatformAccount.platform).distinct().all()
    present = {r[0] for r in rows}
    return [p for p in (Platform.wb, Platform.ozon, Platform.kit) if p in present]


def collect_missing(db: Session, platform: Platform | None = None,
                    limit: int = PAGE_LIMIT) -> tuple[list[MissingRow], int]:
    """Товары с остатком, которых нет на площадке. (строки, сколько всего).

    `platform=None` — показать те, которых нет хотя бы на одной площадке.

    Остаток строго больше нуля: товар, которого нет на складе, заводить на
    площадку незачем, и попади он сюда — список стал бы каталогом всей
    номенклатуры, то есть бесполезным.
    """
    used = platforms_in_use(db)
    wanted = [platform] if platform is not None else used
    wanted = [p for p in wanted if p in used]
    if not wanted:
        return [], 0

    have = {p: _uids_with_card(db, p) for p in wanted}

    rows: list[MissingRow] = []
    total = 0
    # КОЛОНКИ, а не объекты Product. Пройти надо по всему складу — иначе «всего»
    # будет неправдой, — а это сто пятьдесят тысяч строк. Собирать из них
    # полноценные объекты ORM (со всей обвязкой отслеживания изменений) стоило
    # почти трёх секунд на страницу; нужны отсюда шесть полей.
    query = (
        db.query(Product.uid_1c, Product.article, Product.name, Product.size,
                 Product.color, Product.stock_on_hand)
        .filter(Product.stock_on_hand > 0)
        .order_by(Product.article, Product.name)
    )
    for uid, article, name, size, color, stock in query.yield_per(1000):
        missing = {p: uid not in have[p] for p in wanted}
        if not any(missing.values()):
            continue
        total += 1
        if len(rows) < limit:
            rows.append(MissingRow(
                uid_1c=uid, article=article or "", name=name or "",
                size=size or "", color=color or "", stock=stock or 0,
                missing=missing,
            ))
    return rows, total
