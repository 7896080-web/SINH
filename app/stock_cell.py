"""Ячейка остатка площадки: кто ещё делит одну карточку и сколько туда уедет.

Один и тот же физический товар бывает заведён в 1С двумя номенклатурными
строками под разными артикулами, а на площадке это ОДНА карточка одного
размера. Случай редкий, но живой: 26.09 на бою `4033 Неро Джемпер 50/50
SIYAH` и `4052 (4033) b 3XL SIYAH` оказались одной вещью, и обе строки вели
на один размер карточки WB.

Без этого модуля они писали в одну ячейку по очереди, и выигрывал
написавший последним: у одного уходило 10, у второго 18, на площадке лежало
то одно, то другое. Разобрать это по одной строке нельзя никогда — каждая
про свой товар и каждая по-своему права, — а следствие двустороннее: лежит
меньшее число, значит часть товара не продаётся; лежит большее — продаётся
то, чего по этой строке нет.

Правильное число — СУММА: физически на складе лежит столько, сколько дают
обе строки вместе, и покупателю всё равно, какой из наших артикулов ему
отгрузят.

Три решения, которые здесь приняты и которые стоит знать.

**Единица — строка каталога кабинета (`external_id`), а не ключ отправки.**
У WB ключ отправки — баркод, а в теле запроса уезжает chrtId, и выбирает его
сам клиент. Два РАЗНЫХ баркода, ведущие на один размер карточки, по ключу
отправки выглядят как разные ячейки — сложение не сработало бы ровно там,
ради чего затевалось. `external_id` у WB это `nmID:chrtID`, то есть прямо
размер карточки; у Kit — variant_id; у Ozon — product_id. Каталога нет —
`external_id` пуст, и ячейкой служит сам ключ отправки: соседей мы всё равно
найти не можем, а `Barcode.barcode` уникален, так что одиночка остаётся
одиночкой.

**Слагаемые берутся из БАЗЫ, а не из очереди.** Рассылка событийная: в цикле
может лежать запись только по одному из двух товаров — второй не менялся и
меняться не будет месяцами. Сложи мы только то, что в очереди, ячейка
получила бы вклад одной строки вместо двух, то есть остаток соседа молча
исчез бы с площадки.

**Вклад заблокированного выключателем товара — НОЛЬ, а не отказ от отправки.**
У соседа выключена трансляция или снята галочка кабинета — значит этой
половиной мы не управляем, и её число в ячейку не идёт. Ошибка при этом
безопасная: на площадке окажется меньше, чем лежит на складе, — недопродажа,
а не оверселл.
"""
from sqlalchemy.orm import Session

from app.models import Barcode, PlatformCatalogItem, Product
from app.transmit import blocked_by_switch, quantity_for_account


def cell_of(external_id: str, identifier: str) -> str:
    """Ячейка остатка: строка каталога, если она известна, иначе ключ отправки."""
    return (external_id or "").strip() or identifier


def members(db: Session, account_id: int, cell: str, uid_1c: str) -> list[str]:
    """Товары 1С, чей остаток лежит в ЭТОЙ ячейке кабинета. Отсортировано.

    Сам `uid_1c` в ответе всегда: он привёл нас сюда, и потерять его, не найдя
    строк каталога, значило бы не отправить ничего.
    """
    found = {uid_1c}
    rows = db.query(PlatformCatalogItem.barcode).filter(
        PlatformCatalogItem.account_id == account_id,
        PlatformCatalogItem.external_id == cell,
        PlatformCatalogItem.barcode.isnot(None),
    ).all()
    codes = [bc for (bc,) in rows if bc]
    if codes:
        for (uid,) in db.query(Barcode.uid_1c).filter(Barcode.barcode.in_(codes)).all():
            if uid:
                found.add(uid)
    return sorted(found)


def shares(db: Session, account_id: int, uids: list[str],
           raw_stock: dict[str, int] | None = None) -> list[tuple[str, int]]:
    """Вклад каждого товара в ячейку. Порядок тот же, что у `uids`.

    `raw_stock` — остаток, от которого считать, если он уже на руках (у товара
    из очереди это `item.quantity`, посчитанное в момент события). Для соседей
    его нет и быть не может, поэтому берётся текущий `stock_on_hand`.
    """
    raw_stock = raw_stock or {}
    out: list[tuple[str, int]] = []
    for uid in uids:
        if blocked_by_switch(db, uid, account_id):
            out.append((uid, 0))
            continue
        base = raw_stock.get(uid)
        if base is None:
            product = db.query(Product).filter(Product.uid_1c == uid).first()
            base = (product.stock_on_hand or 0) if product is not None else 0
        out.append((uid, quantity_for_account(db, uid, account_id, base)))
    return out


def explain_sum(shares_list: list[tuple[str, int]], labels: dict[str, str]) -> str:
    """Оговорка к отправке: из чего сложилось число. Пишется в `last_error`
    УСПЕШНОЙ записи — иначе сумма выглядит как ошибка расчёта по одной строке,
    и разобраться в ней потом нечем."""
    total = sum(q for _, q in shares_list)
    parts = ", ".join(f"{labels.get(uid, uid)} {q}" for uid, q in shares_list)
    return (f"на карточке несколько товаров 1С — ушла сумма {total} "
            f"({parts})")


def shared_cells(db: Session, uids: list[str],
                 account_ids: list[int]) -> dict[tuple[str, int], list[str]]:
    """Пары (товар, кабинет), чья ячейка занята НЕ ТОЛЬКО этим товаром.

    Значение — соседи по ячейке (без самого товара). Нужно странице «Товары»:
    в строке стоит число ЭТОГО товара, а на карточку уедет сумма, и строка,
    молчащая об этом, показывает не то, что уходит наружу, — отдельный класс
    дефектов этого проекта.

    Собирается ТРЕМЯ запросами на страницу, а не подзапросом на строку: каталог
    боевого кабинета — сто пятьдесят тысяч строк, и обращение на строку уже
    один раз оставило страницу без ответа вовсе.
    """
    if not uids or not account_ids:
        return {}
    own: dict[str, list[str]] = {}
    for bc, uid in db.query(Barcode.barcode, Barcode.uid_1c).filter(
            Barcode.uid_1c.in_(uids)).all():
        own.setdefault(bc, []).append(uid)
    if not own:
        return {}

    # Какие ячейки заняты показанными товарами.
    cells: dict[tuple[int, str], set[str]] = {}
    for acc, ext, bc in db.query(
            PlatformCatalogItem.account_id, PlatformCatalogItem.external_id,
            PlatformCatalogItem.barcode).filter(
            PlatformCatalogItem.account_id.in_(account_ids),
            PlatformCatalogItem.barcode.in_(list(own))).all():
        if ext:
            cells.setdefault((acc, ext), set()).update(own.get(bc, []))
    if not cells:
        return {}

    # Кто ещё в этих ячейках — включая товары, которых на странице нет.
    wanted = {ext for _, ext in cells}
    codes: dict[tuple[int, str], set[str]] = {}
    for acc, ext, bc in db.query(
            PlatformCatalogItem.account_id, PlatformCatalogItem.external_id,
            PlatformCatalogItem.barcode).filter(
            PlatformCatalogItem.account_id.in_(account_ids),
            PlatformCatalogItem.external_id.in_(list(wanted))).all():
        if bc:
            codes.setdefault((acc, ext), set()).add(bc)
    all_codes = {bc for group in codes.values() for bc in group}
    by_barcode: dict[str, str] = {
        bc: uid for bc, uid in db.query(Barcode.barcode, Barcode.uid_1c).filter(
            Barcode.barcode.in_(list(all_codes))).all()}

    out: dict[tuple[str, int], list[str]] = {}
    for (acc, ext), mine in cells.items():
        tenants = {by_barcode[bc] for bc in codes.get((acc, ext), set())
                   if bc in by_barcode}
        for uid in mine:
            others = sorted(tenants - {uid})
            if others:
                out[(uid, acc)] = others
    return out
