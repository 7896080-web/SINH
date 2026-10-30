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
from app.transmit import blocked_by_switch, contribution_to_cell


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
    """Вклад каждого товара в ячейку — СЛАГАЕМЫЕ, а не итог. Порядок — как `uids`.

    `raw_stock` — остаток, от которого считать, если он уже на руках (у товара
    из очереди это `item.quantity`, посчитанное в момент события). Для соседей
    его нет и быть не может, поэтому берётся текущий `stock_on_hand`.

    Слагаемое бывает ОТРИЦАТЕЛЬНЫМ, и это главное про эту функцию. Обрезку
    делает `cell_quantity`, одна на всю ячейку; почему не по слагаемому — см.
    `transmit.contribution_to_cell`.
    """
    raw_stock = raw_stock or {}
    out: list[tuple[str, int]] = []
    for uid in uids:
        base = raw_stock.get(uid)
        if base is None:
            product = db.query(Product).filter(Product.uid_1c == uid).first()
            base = (product.stock_on_hand or 0) if product is not None else 0
        out.append((uid, contribution_to_cell(db, uid, account_id, base)))
    return out


def cell_quantity(shares_list: list[tuple[str, int]]) -> int:
    """Сколько уедет в ячейку. ЕДИНСТВЕННЫЙ хозяин обрезки по итогу.

    Обрезаем один раз и здесь: слагаемые бывают минусовыми (минус соседа —
    мера продаж, которые со склада уже уехали), и обрезка по слагаемому
    отправила бы на карточку больше, чем лежит. Отрицательная СУММА наружу всё
    равно не уедет — для площадки ноль и есть «не продавать», — но вычесть её
    из вклада соседа обязана.

    Зовут эту функцию трое: рассылка, страница «Товары» и `probe_pool.py`.
    Повтори кто-нибудь `sum(...)` у себя — страница показывала бы не то число,
    которое уйдёт наружу, а это отдельный класс дефектов этого проекта.
    """
    return max(0, sum(q for _, q in shares_list))


def explain_sum(shares_list: list[tuple[str, int]], labels: dict[str, str]) -> str:
    """Оговорка к отправке: из чего сложилось число. Пишется в `last_error`
    УСПЕШНОЙ записи — иначе сумма выглядит как ошибка расчёта по одной строке,
    и разобраться в ней потом нечем.

    Про обрезку говорит ОТДЕЛЬНО. Сумма слагаемых ушла в минус — наружу уехал
    ноль, и строка «ушла сумма 0» рядом со слагаемыми 40 и −43 читается как
    ошибка расчёта: человек идёт искать, почему не ушло 40.
    """
    total = sum(q for _, q in shares_list)
    sent = cell_quantity(shares_list)
    parts = ", ".join(f"{labels.get(uid, uid)} {q}" for uid, q in shares_list)
    if sent != total:
        return (f"на карточке несколько товаров 1С — сумма слагаемых {total}, "
                f"то есть минус: уехал ноль ({parts})")
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


def managed_neighbours(db: Session, pairs: list[tuple[str, int]]
                       ) -> dict[tuple[str, int], list[str]]:
    """Пары, у которых сосед по ячейке НЕ заблокирован выключателем.

    Вопрос узкий и нужен ровно в одном месте — у находки про пару без галочки
    (`report._check_unmanaged_pairs`), — но ответ меняет и следствие, и
    лекарство, поэтому живёт он здесь, рядом с самой ячейкой.

    Пара без галочки сама наружу не отправляет ничего, и если в ячейке она
    одна, число на карточке — остаток прежних времён: его никто не обновляет,
    он убывает с каждой продажей площадки и однажды кончится. Это и есть
    находка.

    А вот если рядом по той же карточке стоит УПРАВЛЯЕМЫЙ товар, всё в порядке
    и находки нет: два артикула 1С на одной карточке — штатная расстановка,
    партии одной модели идут с пересекающимися размерами (4033 M–3XL и 4052
    3XL–6XL делят 3XL; 2403 и пришедшая за ней 2503 — один товар), а пул
    баркодов сопоставлен верно по цветоразмеру. Число на такой карточке наше и
    ПРАВИЛЬНОЕ: заказ списывается с той партии, где лежит остаток
    (`payer_for`), а минус распроданной вычитается из суммы (`cell_quantity`).
    Сутки (pm173) у этого случая была своя КРИТИЧНАЯ находка, обещавшая
    самоподдерживающийся оверселл; с обрезкой по итогу суммы обещание стало
    неправдой, и находка убрана — проверка, кричащая на штатной работе,
    кончается тем, что отчёт пролистывают целиком.

    Спрашивается это ПРИ ЧТЕНИИ, а не при записи аномалии: галочку соседу могли
    поставить или снять после, и находка обязана описывать нынешнее положение.
    Тот же приём, что у `report._only_live_pairs`.
    """
    if not pairs:
        return {}
    uids = sorted({uid for uid, _ in pairs})
    accounts = sorted({acc for _, acc in pairs})
    neighbours = shared_cells(db, uids, accounts)
    out: dict[tuple[str, int], list[str]] = {}
    for pair in pairs:
        others = neighbours.get(pair)
        if not others:
            continue
        _uid, account_id = pair
        live = [u for u in others if not blocked_by_switch(db, u, account_id)]
        if live:
            out[pair] = live
    return out


def payer_for(db: Session, uid_1c: str, account_id: int,
              quantity: int) -> tuple[str, str | None]:
    """Кто ФАКТИЧЕСКИ платит за этот заказ: товар баркода или сосед по ячейке.

    Возвращает (кто списывается, кого баркод назвал — если подменили, иначе
    None).

    29.10 на бою: `2503 SIYAH` и `2403 SIYAH` ведут на одну карточку. Остаток
    лежит на 2503, баркод карточки привязан к 2403, у 2403 ноль. Каждый заказ
    списывался с 2403 — тот уходил в МИНУС, — а у 2503 не убавлялось вовсе, и
    рассылка отправляла его полное число снова и снова: оверселл, который сам
    не кончится никогда. Физически же со склада уехала единица 2503, и списать
    её надо с 2503.

    Правило одно, и обычный путь им НЕ ЗАДЕВАЕТСЯ: товар баркода покрывает
    заказ — он и платит, ни запроса соседей, ни подмены. Подмена включается
    ровно там, где иначе ушли бы в минус.

    Сосед выбирается по ОСТАТКУ, а не по галочкам: вопрос «чья единица уехала
    со склада» к трансляции отношения не имеет, и ограничь мы выбор
    отмеченными парами, подмена не сработала бы ровно в том случае, ради
    которого написана (у 2403 галочки и нет). Берём того, у кого остаток
    больше, при равенстве — первого по uid: подмена обязана быть
    воспроизводимой, иначе один и тот же заказ списывался бы то с одного, то с
    другого.

    Никто не покрывает — НЕ ПОДМЕНЯЕМ: пусть уходит в минус, как и раньше.
    Минус тогда честно значит «отгрузили больше, чем числилось», и у него есть
    читатель (`report._check_negative_stock`). Выдумать плательщика там, где
    его нет, значило бы списать с товара, которого тоже нет.

    **Цена ошибки несимметрична, и поэтому подмена уместна даже если ячейка
    образовалась ОШИБКОЙ мэппинга** (два разных товара на одной карточке).
    Без подмены: минус у одного, завышенный остаток у второго, карточка продаёт
    его полное число — оверселл, и сам он не исправится. С подменой: списали у
    соседа единицу, которая, может, и не уезжала, — недопродажа, и её поправит
    ближайшая сверка с 1С, потому что она применяет снимок 1С как есть. Из двух
    ошибок выбираем ту, что дешевле и самолечится.
    """
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    if product is not None and (product.stock_on_hand or 0) >= quantity:
        return uid_1c, None

    codes = [bc for (bc,) in db.query(Barcode.barcode).filter(
        Barcode.uid_1c == uid_1c).all() if bc]
    if not codes:
        return uid_1c, None
    cells = {ext for (ext,) in db.query(PlatformCatalogItem.external_id).filter(
        PlatformCatalogItem.account_id == account_id,
        PlatformCatalogItem.barcode.in_(codes),
    ).all() if ext}
    if not cells:
        return uid_1c, None

    neighbours: set[str] = set()
    for cell in cells:
        neighbours.update(members(db, account_id, cell, uid_1c))
    neighbours.discard(uid_1c)
    if not neighbours:
        return uid_1c, None

    able = [p for p in db.query(Product).filter(
        Product.uid_1c.in_(sorted(neighbours))).all()
        if (p.stock_on_hand or 0) >= quantity]
    if not able:
        return uid_1c, None
    able.sort(key=lambda p: (-(p.stock_on_hand or 0), p.uid_1c))
    return able[0].uid_1c, uid_1c
