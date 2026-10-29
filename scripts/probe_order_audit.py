"""Что сделала бы суточная сверка заказов — БЕЗ того, чтобы делать.

Запуск:

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_order_audit.py
    ... probe_order_audit.py --cabinet 3
    ... probe_order_audit.py --orders            # с номерами заказов построчно

Зачем. Сверка (`app/workers/order_audit.py`) ПРОВОДИТ найденное: списывает
остаток и создаёт перемещение в 1С. Это осознанный выбор — названная, но не
проведённая продажа оставляет остаток завышенным до того, как человек дойдёт до
списка, — но сколько заказов живой опрос потерял за ВСЁ время, заранее не знает
никто: это и есть то, что сверка выясняет. Потолок на прогон объём ограничивает,
а ПОКАЗАТЬ его до первого прогона было нечем. Этот скрипт и показывает.

Отбор он не повторяет, а ЗОВЁТ (`order_audit.plan_account`): разойдись он со
сверкой, он однажды согласился бы ровно там, где нужен, — а решают по нему, не
слишком ли велик первый прогон.

А вот РАЗБИВКУ отобранного на «уедет в 1С» и «пара не отмечена» он выводит САМ,
прямым запросом к баркодам и настройкам, и это осознанное исключение из правила
«у формулы один хозяин». Боевой путь спрашивает `resolve_barcode`, а тот ПИШЕТ:
незнакомый баркод оседает конфликтом сопоставления. Скрипт, запускаемый ради
«посмотреть, ничего не меняя», такого делать не имеет права. Поэтому разбивка —
ПРЕДСКАЗАНИЕ, и скрипт говорит это вслух: на незнакомых баркодах она может
разойтись с тем, что получится на самом деле.

Ничего не меняет, в 1С и на площадки не шлёт.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                               # noqa: E402
from app.models import (Barcode, PlatformAccount, Product,          # noqa: E402
                        SyncSetting)
from app.workers.client_factory import build_client                 # noqa: E402
from app.workers.order_audit import (AUDIT_WINDOW_DAYS,             # noqa: E402
                                     MAX_PER_ACCOUNT, plan_account)

# Консоль боевого сервера пишет в cp1251, и один символ, которого в ней нет,
# роняет ВЕСЬ вывод посреди строки. Свои строки мы держим в её пределах
# (закрыто тестом), но сюда печатаются ЧУЖИЕ данные: имена кабинетов, номера
# заказов с площадки, тела ответов.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):      # перенаправленный вывод, старый Python
    pass


def _enabled_uids(db, barcodes: list[str], account_id: int) -> tuple[set[str], set[str]]:
    """(баркоды отмеченных пар, баркоды, которых нет в «Баркодах»).

    Прямым запросом, без `resolve_barcode`: тот при незнакомом баркоде ЗАВОДИТ
    конфликт сопоставления, а скрипт обязан ничего не менять. Цена — предсказание
    вместо обещания, и об этом сказано в выводе.
    """
    if not barcodes:
        return set(), set()
    rows = {}
    chunk = 500
    for start in range(0, len(barcodes), chunk):
        part = barcodes[start:start + chunk]
        for b in db.query(Barcode).filter(Barcode.barcode.in_(part)).all():
            rows[b.barcode] = b.uid_1c
    unknown = {b for b in barcodes if b not in rows}
    uids = set(rows.values())
    on = {s.uid_1c for s in db.query(SyncSetting).filter(
        SyncSetting.account_id == account_id,
        SyncSetting.uid_1c.in_(list(uids)) if uids else SyncSetting.uid_1c.is_(None),
        SyncSetting.enabled.is_(True),
    ).all()} if uids else set()
    enabled = {b for b, uid in rows.items() if uid in on}
    return enabled, unknown


def _products_for(db, barcodes: list[str]) -> dict:
    """{баркод: товар} одним запросом на пачку."""
    if not barcodes:
        return {}
    pairs = {}
    chunk = 500
    for start in range(0, len(barcodes), chunk):
        part = barcodes[start:start + chunk]
        for b in db.query(Barcode).filter(Barcode.barcode.in_(part)).all():
            pairs[b.barcode] = b.uid_1c
    uids = list(set(pairs.values()))
    products = {}
    for start in range(0, len(uids), chunk):
        part = uids[start:start + chunk]
        for product in db.query(Product).filter(Product.uid_1c.in_(part)).all():
            products[product.uid_1c] = product
    return {code: products.get(uid) for code, uid in pairs.items()}


def _label(product) -> str:
    if product is None:
        return "товара в номенклатуре нет"
    parts = [(product.article or "").strip(), (product.size or "").strip(),
             (product.color or "").strip(), (product.name or "").strip()]
    return " ".join(p for p in parts if p) or (product.uid_1c or "?")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    want_orders = "--orders" in argv
    argv = [a for a in argv if a != "--orders"]
    only = None
    if "--cabinet" in argv:
        i = argv.index("--cabinet")
        if i + 1 >= len(argv):
            print("--cabinet требует номер кабинета")
            return 2
        only = argv[i + 1]

    db = SessionLocal()
    try:
        accounts = db.query(PlatformAccount).filter(
            PlatformAccount.is_active.is_(True)).order_by(PlatformAccount.id).all()
        if only is not None:
            accounts = [a for a in accounts if str(a.id) == only or a.name == only]
            if not accounts:
                print(f"Активного кабинета «{only}» нет.")
                return 1
        if not accounts:
            print("Активных кабинетов нет — сверять нечего.")
            return 0

        print(f"Окно сверки: {AUDIT_WINDOW_DAYS} суток. Потолок на прогон: "
              f"{MAX_PER_ACCOUNT} заказов на кабинет.")
        print("НИЧЕГО не проводится: это предсказание, а не прогон.")
        print("")

        grand = {"seen": 0, "known": 0, "cancelled": 0, "would": 0,
                 "disabled": 0, "unknown": 0}
        for account in accounts:
            print("=" * 78)
            print(f"Кабинет #{account.id} {account.name} ({account.platform.value})")
            print("=" * 78)
            try:
                client = build_client(db, account.id)
                candidates, stats = plan_account(db, client, account)
            except Exception as e:
                # Площадки независимы: молчание WB — не повод не посмотреть Kit.
                print(f"  ОТКАЗ: {type(e).__name__}: {e}")
                print("")
                continue

            codes = [o.barcode for o in candidates if o.barcode]
            enabled, unknown = _enabled_uids(db, codes, account.id)
            would = [o for o in candidates if o.barcode in enabled]
            unmatched = [o for o in candidates if o.barcode in unknown]
            disabled = len(candidates) - len(would) - len(unmatched)

            print(f"  заказов в ленте:               {stats['seen']}")
            print(f"  из них уже есть у нас:         {stats['known']}")
            print(f"  отменены площадкой (пропуск):  {stats['cancelled']}")
            print(f"  ОТОБРАНО:                      {len(candidates)}")
            print(f"    уедет в 1С (пара отмечена):  {len(would)}")
            print(f"    пропустит (пара не отмечена):{disabled}")
            print(f"    баркод не сопоставлен:       {len(unmatched)}")
            if len(would) > MAX_PER_ACCOUNT:
                print(f"  ВНИМАНИЕ: больше потолка — за прогон уедет "
                      f"{MAX_PER_ACCOUNT}, остальные {len(would) - MAX_PER_ACCOUNT} "
                      f"в следующие сутки.")
            for problem in stats["problems"]:
                print(f"  оговорка: {problem}")
            if want_orders:
                # Называем ТОВАР, а не только баркод: по баркоду человек строку
                # не узнаёт, а решает он именно по этому списку — верно ли
                # сверка собралась действовать. Названия берём одним запросом
                # на кабинет, а не подзапросом на заказ.
                #
                # И печатаем ПРОПУЩЕННЫЕ тоже. На бою 29.10 к проведению не
                # вышло ни одного заказа, а отобранных было двенадцать — и
                # посмотреть, что это за товары, было НЕЧЕМ: строка «пропустит
                # 12» без имён читается как «двенадцать чужих», тогда как среди
                # них может лежать позиция, которую вот-вот включают. Включат —
                # и заказ уже выпал из тридцатисуточного окна, то есть сверка
                # его не догонит никогда (догонит «Расчёт», но только если
                # человек про него знает). Молчать тут значит отвечать не на
                # тот вопрос, с которым скрипт открывают.
                named = _products_for(db, [o.barcode for o in candidates])
                for o in would:
                    print(f"    -> {o.order_id} {_label(named.get(o.barcode))} "
                          f"баркод {o.barcode} кол-во {o.quantity} "
                          f"от {o.order_date}")
                for o in candidates:
                    if o.barcode in enabled:
                        continue
                    # Причина у двух корзин разная, и разбирают их по-разному:
                    # «пара не отмечена» — это решение человека, «баркод не
                    # сопоставлен» — работа на «Мэппинге».
                    why = ("баркод не сопоставлен" if o.barcode in unknown
                           else "пара не отмечена")
                    print(f"    (пропуск: {why}) {o.order_id} "
                          f"{_label(named.get(o.barcode))} баркод {o.barcode} "
                          f"кол-во {o.quantity} от {o.order_date}")
            print("")

            grand["seen"] += stats["seen"]
            grand["known"] += stats["known"]
            grand["cancelled"] += stats["cancelled"]
            grand["would"] += len(would)
            grand["disabled"] += disabled
            grand["unknown"] += len(unmatched)

        print("=" * 78)
        print("ИТОГО")
        print("=" * 78)
        print(f"  заказов в лентах:              {grand['seen']}")
        print(f"  уже есть у нас:                {grand['known']}")
        print(f"  отменены площадкой:            {grand['cancelled']}")
        print(f"  УЕДЕТ В 1С перемещениями:      {grand['would']}")
        print(f"  пропустит (пары не отмечены):  {grand['disabled']}")
        print(f"  баркод не сопоставлен:         {grand['unknown']}")
        print("")
        print("Разбивка отобранного — ПРЕДСКАЗАНИЕ: боевой путь спрашивает")
        print("resolve_barcode, который умеет связывать баркод через каталог")
        print("площадки и заводить конфликт сопоставления, а скрипт ничего не")
        print("меняет и потому смотрит только таблицу «Баркоды». На незнакомых")
        print("баркодах числа «уедет» и «не сопоставлен» могут разойтись с тем,")
        print("что получится на самом деле.")
        print("")
        if grand["disabled"] or grand["unknown"]:
            print("Что именно пропустит — покажет тот же скрипт с ключом --orders:")
            print("среди пропущенных бывает позиция, которую вот-вот включают, и")
            print("тогда отметить пару стоит ДО того, как заказ выпадет из окна.")
            print("")
        print("Ноль в строке «УЕДЕТ В 1С» НЕ значит «сверка не нужна»: он значит")
        print("только, что прямо сейчас догонять нечего. Потери появляются тогда,")
        print("когда кабинет гаснет или площадка молчит, и до следующего прогона")
        print("о них не узнает никто.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
