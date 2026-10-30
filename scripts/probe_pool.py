"""Пул баркодов одной карточки площадки: кто её делит и сойдётся ли отправка.

Запуск:

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_pool.py 2000932130002
    ... probe_pool.py "4033 Неро" 3XL SIYAH
    ... probe_pool.py 2000932130002 --all-cabinets

Зачем. Один физический товар бывает заведён в 1С ДВУМЯ строками, а на площадке
это ОДНА карточка одного размера. Рассылка складывает их остатки
(`app/stock_cell.py`), и пока склад сходится, всё работает молча. Разбирать
такое по одной строке нельзя никогда: каждая про свой товар и каждая по-своему
права, а снаружи это выглядит как отказ площадки («отправили 10, держит 28»).

Правил ДВА, они живут в разных местах и ломаются порознь:

  * **ячейка остатка** (`stock_cell.members`): соседи — товары 1С, чьи баркоды
    стоят в каталоге ЭТОГО кабинета под тем же `external_id`. Вклад товара,
    заблокированного выключателем (трансляция выключена, кабинет не отмечен),
    равен НУЛЮ, а не отменяет отправку: ошибка в эту сторону даёт недопродажу,
    а не оверселл;
  * **однозначность пула** (`catalog_sync`, `POOL_GUESS_SOURCE`): незнакомый
    баркод привязывается к товару 1С, только если ВСЕ баркоды его пула ведут к
    ОДНОМУ uid. Неоднозначный пул не мержится вовсе — новые баркоды такой
    карточки уходят в конфликты сопоставления и остаются непривязанными.

Первое правило скрипт не повторяет, а ЗОВЁТ (`cell_of`, `members`, `shares`,
`_resolve_push_target`): разойдись он с рассылкой, он однажды согласился бы
ровно там, где нужен. Второе выводит САМ, обычной арифметикой, — и это
осознанное исключение: предмет проверки здесь и есть само правило, а позови
скрипт тот же код, он согласился бы с ним всегда, что бы в нём ни сломалось.
Тот же приём, что в `scripts/probe_offset_invariant.py`.

Поиск строк — ОДИН на все probe-скрипты (`probe_movements._find_rows`):
человек бежит между ними с одной и той же строкой в буфере, и разойдись они,
один и тот же вызов отвечал бы по-разному.

Ничего не меняет, в 1С и на площадки не шлёт.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                              # noqa: E402
from app.models import (Barcode, PlatformAccount,                   # noqa: E402
                        PlatformCatalogItem, Product, SyncSetting)
from app.stock_cell import cell_of, cell_quantity, members, shares  # noqa: E402
from app.workers.dispatch import _resolve_push_target               # noqa: E402
from probe_movements import _find_rows, _size_key                   # noqa: E402

# Консоль боевого сервера пишет в cp1251, и один символ, которого в ней нет,
# роняет ВЕСЬ вывод посреди строки. Свои строки мы держим в её пределах
# (закрыто тестом), но сюда печатаются ЧУЖИЕ данные: названия товаров,
# артикулы с площадки, имена кабинетов.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):      # перенаправленный вывод, старый Python
    pass


def _label(product) -> str:
    if product is None:
        return "?"
    return (product.article or product.uid_1c or "?").strip()


def _accounts_in_play(db, uids: list[str], every: bool) -> list[PlatformAccount]:
    """Кабинеты, про которые есть что сказать.

    Берём те, где товар ОТМЕЧЕН, и те, в чьём каталоге лежит хоть один его
    баркод. Второе обязательно: карточка в каталоге есть, а галочки нет — это
    и есть случай, когда сосед молча перестал участвовать в сумме.
    """
    if every:
        return db.query(PlatformAccount).order_by(PlatformAccount.id).all()
    codes = [b.barcode for b in db.query(Barcode).filter(Barcode.uid_1c.in_(uids)).all()]
    ids = {s.account_id for s in db.query(SyncSetting).filter(SyncSetting.uid_1c.in_(uids)).all()}
    if codes:
        ids |= {r.account_id for r in db.query(PlatformCatalogItem).filter(
            PlatformCatalogItem.barcode.in_(codes)).all()}
    if not ids:
        return []
    return (db.query(PlatformAccount)
            .filter(PlatformAccount.id.in_(sorted(ids)))
            .order_by(PlatformAccount.id).all())


def _pool_owners(db, account_id: int, cell: str) -> tuple[list[tuple[str, str]], set[str]]:
    """Баркоды пула и товары 1С, к которым они привязаны.

    Возвращает (список пар баркод->uid или пустой uid, множество разных uid).
    Пустой uid значит «баркод в каталоге площадки есть, в 1С его нет».
    """
    codes = [bc for (bc,) in db.query(PlatformCatalogItem.barcode).filter(
        PlatformCatalogItem.account_id == account_id,
        PlatformCatalogItem.external_id == cell,
        PlatformCatalogItem.barcode.isnot(None)).all() if bc]
    linked = {b.barcode: b.uid_1c for b in db.query(Barcode).filter(
        Barcode.barcode.in_(codes)).all()} if codes else {}
    pairs = sorted((bc, linked.get(bc, "")) for bc in codes)
    return pairs, {uid for uid in linked.values() if uid}


def _say_cabinet(db, product, account, verdicts: dict) -> None:
    print(f"  КАБИНЕТ {account.id} ({account.name}, {account.platform.value})"
          + ("" if account.is_active else "   ВЫКЛЮЧЕН"))

    target = _resolve_push_target(db, product.uid_1c, account.id)
    if target is None:
        print("    у товара нет баркодов вовсе — отправлять не по чему")
        verdicts["no_barcode"] += 1
        return
    barcode, external_id, _article = target
    if not external_id:
        # Каталог кабинета не загружен или карточки в нём нет. Соседей мы в
        # этом случае найти не можем в принципе, и ячейкой служит сам ключ —
        # одиночка остаётся одиночкой.
        print(f"    карточки в каталоге кабинета НЕТ (искали по баркоду {barcode})")
        print("      -> соседей не видно НИКАКИХ, уедет только своё число;")
        print("         если на карточке есть второй товар 1С, его остаток")
        print("         туда не попадёт. Лечится загрузкой каталога кабинета.")
        verdicts["no_card"] += 1
        return

    cell = cell_of(external_id, barcode)
    print(f"    ячейка (external_id): {cell}")

    pairs, uids = _pool_owners(db, account.id, cell)
    print(f"    баркодов в пуле: {len(pairs)}")
    unlinked = 0
    for bc, uid in pairs:
        owner = db.query(Product).filter(Product.uid_1c == uid).first() if uid else None
        if not uid:
            unlinked += 1
            mark = "В 1С НЕ ПРИВЯЗАН"
        elif uid == product.uid_1c:
            mark = "наш товар"
        else:
            mark = f"{_label(owner)}  {(owner.size or '-') if owner else '-'}" \
                   f" / {(owner.color or '-') if owner else '-'}"
        print(f"      {bc}  -> {mark}")

    # Правило 1: кого рассылка считает жильцами ячейки и сколько даст каждый.
    cell_uids = members(db, account.id, cell, product.uid_1c)
    parts = shares(db, account.id, cell_uids)
    total = cell_quantity(parts)
    raw_total = sum(q for _, q in parts)
    if len(cell_uids) > 1:
        verdicts["shared"] += 1
        print(f"    ЖИЛЬЦОВ {len(cell_uids)} — на карточку уедет СУММА {total}:")
    else:
        print(f"    жилец один, уедет {total}")
    for uid, qty in parts:
        owner = db.query(Product).filter(Product.uid_1c == uid).first()
        stock = (owner.stock_on_hand or 0) if owner else 0
        note = ""
        if qty < 0:
            # Минус в слагаемом — это продажи, которые со склада уехали, а в
            # учёте не отражены. Он ВЫЧИТАЕТСЯ из суммы намеренно, и промолчи
            # скрипт, человек читал бы «уедет 33» при остатке соседа 40 и не
            # понял бы, откуда взялось число.
            note = "   <- МИНУС, вычитается: отгрузили больше, чем числилось"
            verdicts["negative_member"] += 1
        elif qty == 0 and stock > 0:
            note = "   <- вклад НОЛЬ при остатке " + str(stock) + \
                   ": выключатель (трансляция или галочка кабинета)"
            verdicts["muted_neighbour"] += 1
        print(f"      {_label(owner):28} {qty:>4}{note}")
    if raw_total != total:
        # Обрезка по итогу. Молчи о ней скрипт, и «уедет 0» при жильце с
        # остатком 40 выглядело бы поломкой расчёта, а не минусом соседа.
        print(f"    сумма слагаемых {raw_total} — это минус, наружу уедет ноль")
        verdicts["clamped"] += 1

    # Правило 2: однозначен ли пул для догадки `catalog_sync`.
    if len(uids) > 1:
        verdicts["ambiguous"] += 1
        print(f"    ПУЛ НЕОДНОЗНАЧЕН: баркоды ведут к {len(uids)} разным товарам 1С")
        print("      -> новые баркоды этой карточки автопривязка НЕ возьмёт,")
        print("         они уйдут в конфликты сопоставления и останутся висеть")
    elif unlinked:
        print(f"    в пуле {unlinked} баркодов без привязки, но пул однозначен")
        print("      -> ближайшая загрузка каталога привяжет их к нашему товару")


def main() -> int:
    args = [a for a in (arg.strip() for arg in sys.argv[1:]) if a]
    every = "--all-cabinets" in args
    args = [a for a in args if a != "--all-cabinets"]
    if not args:
        print("укажите баркод, ID_1С или артикул; далее размер и/или цвет")
        print("пример: probe_pool.py 2000932130002")
        return 2

    db = SessionLocal()
    try:
        rows = _find_rows(db, args[0], args[1:])
        if not rows:
            return 1
        rows.sort(key=_size_key)
        print("=" * 78)
        print(f"АРТИКУЛ: {rows[0].article or '-'}")
        print(f"Название: {rows[0].name or '-'}")
        print(f"Строк в разборе: {len(rows)}")
        print("=" * 78)
        print("Ячейка остатка — это СТРОКА КАТАЛОГА кабинета (external_id),")
        print("а не ключ отправки: у WB ключ баркод, а в теле уезжает chrtId,")
        print("и два разных баркода одного размера по ключу выглядели бы")
        print("разными ячейками.")

        verdicts = defaultdict(int)
        accounts = _accounts_in_play(db, [p.uid_1c for p in rows], every)
        if not accounts:
            print("")
            print("Ни одного кабинета: товар нигде не отмечен и в каталогах его нет.")
            return 0
        for product in rows:
            print("")
            print("-" * 78)
            print(f"РАЗМЕР {product.size or '-':8} цвет {product.color or '-':22}"
                  f" остаток {product.stock_on_hand or 0}")
            print("-" * 78)
            for account in accounts:
                _say_cabinet(db, product, account, verdicts)

        print("")
        print("=" * 78)
        print("ИТОГО")
        print("=" * 78)
        print(f"  карточек с несколькими жильцами: {verdicts['shared']}")
        print(f"  неоднозначных пулов:             {verdicts['ambiguous']}")
        print(f"  соседей с нулевым вкладом:       {verdicts['muted_neighbour']}")
        print(f"  слагаемых в минусе:              {verdicts['negative_member']}")
        print(f"  ячеек, обрезанных до нуля:       {verdicts['clamped']}")
        print(f"  пар без карточки в каталоге:     {verdicts['no_card']}")
        print(f"  строк без баркодов вовсе:        {verdicts['no_barcode']}")
        print("")
        print("Нули тут НЕ значат «всё хорошо»: ноль жильцов с несколькими")
        print("товарами на карточке бывает и когда каталог кабинета устарел и")
        print("соседа в нём больше нет. Тогда две строки 1С снова пишут в одну")
        print("ячейку по очереди, и выигрывает написавший последним.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
