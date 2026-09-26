"""Кто отправлял НУЛИ на кабинет и почему — только чтение.

Запуск (окно по умолчанию — двое суток):

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_zero_sends.py КИТ
    ... probe_zero_sends.py КИТ 6          # только за последние 6 часов
    ... probe_zero_sends.py все 24         # по всем кабинетам сразу
    ... probe_zero_sends.py КИТ 24 3030    # только по модели 3030

Зачем отдельный скрипт, когда есть `probe_offset.py`. Тот отвечает про ОДНУ
строку: «что было с этим товаром». А вопрос «на площадке были остатки, потом
стали нули» — про кабинет ЦЕЛИКОМ: пока не видно, одна это карточка или
шестьсот, разбирать нечего. Одну строку можно посмотреть и после, а вот
СКОЛЬКО их и ОДНИМ ли событием они обнулились — видно только сводкой.

Что печатается и почему именно это.

**Разбивка по `reason`** — главное. Причина постановки в очередь и есть ответ
на «кто это сделал»: `broadcast_off` и `broadcast_toggled` значит человек,
`order` — продажа, `reconciliation` — часовая сверка с 1С, `excel_import` —
залитый файл, `bulk_edit` — массовая кнопка. Число рядом отвечает на второй
вопрос: единичный случай или весь каталог разом.

**Ненулевые отправки за то же окно** — рядом и обязательно. Без них сводка
нулей выглядит одинаково и когда сломалось всё, и когда кабинет просто живёт
обычной жизнью: распроданные позиции дают нули каждый день, это норма.

**Ключи, на которые ведут два и более товара** — отдельным разделом. У Kit
пара «товар+склад» не может повторяться, а два товара 1С МОГУТ вести на одну
карточку площадки: тогда они пишут по очереди, и последний выигрывает — у
одного остаток 20, у второго 0, на витрине 0. Снаружи это выглядит как «сами
обнулились», и по одной строке не находится НИКОГДА: каждая из них про свой
товар, и каждая по-своему права. Считается раздел по ключу ТЕЛА ЗАПРОСА, а не
по `sent_sku`: у WB там лежит баркод, а уезжает chrtId, и два РАЗНЫХ баркода,
ведущие на один размер карточки, по `sent_sku` выглядят как разные ключи —
раздел молчал бы ровно там, где он и нужен.

Ничего не меняет и не коммитит.
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import (AuditLog, DispatchQueueItem, Platform,  # noqa: E402
                        PlatformAccount, Product, SyncSetting)
from app.workers.dispatch import _resolve_push_target                  # noqa: E402
from app.workers.platform_clients.base import StockPushItem            # noqa: E402
from app.workers.platform_clients.wb import WbClient                   # noqa: E402
from app.transmit import explain, sku_mode, MODE_OFFSET, MODE_OVERRIDE  # noqa: E402
from app.database import SessionLocal                                  # noqa: E402
from app.timeutils import now_utc                                      # noqa: E402

# Консоль боевого сервера пишет в cp1251, и один символ, которого в ней нет,
# роняет ВЕСЬ вывод скрипта посреди строки — с `UnicodeEncodeError` вместо
# ответа на вопрос, ради которого скрипт и запускали. Свои строки мы держим в
# пределах cp1251 (закрыто тестом), но сюда печатаются и ЧУЖИЕ данные: тело
# ответа площадки из `last_error`, названия товаров, артикулы. Там может
# оказаться что угодно, и заменить символ на «?» несравнимо лучше, чем не
# напечатать ничего.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):  # перенаправленный вывод, старый Python
    pass


DEFAULT_HOURS = 48
SHOW_ROWS = 40


def _accounts(db, needle: str) -> list[PlatformAccount]:
    """Кабинеты по имени, куску имени или id. «все» — все сразу."""
    if needle.lower() in ("все", "all", "*"):
        return db.query(PlatformAccount).order_by(PlatformAccount.id).all()
    if needle.isdigit():
        found = db.query(PlatformAccount).filter(
            PlatformAccount.id == int(needle)).all()
        if found:
            return found
    return db.query(PlatformAccount).filter(
        PlatformAccount.name.ilike(f"%{needle}%")).order_by(PlatformAccount.id).all()


def _why_empty(product) -> str:
    """Отчего лестница отдала ноль, когда выключатели ни при чём.

    Числами, а не словом: «порог 6 при остатке 5» человек проверяет сам и
    сразу, а «порог» без чисел заставляет лезть в строку товара.
    """
    if product is None:
        return "товара нет в номенклатуре"
    stock = product.stock_on_hand or 0
    mode = sku_mode(product)
    if mode == MODE_OVERRIDE:
        return f"ручной остаток {product.transmit_override}"
    if mode == MODE_OFFSET:
        return (f"порог {product.broadcast_offset} при остатке {stock}"
                f" (расхождение {product.stock_discrepancy} + бронь {product.reserve or 0})")
    if stock <= 0:
        return f"остаток {stock} — распродано"
    return f"остаток {stock} минус бронь {product.reserve or 0}"


def _label(products: dict, uid: str) -> str:
    p = products.get(uid)
    if p is None:
        return f"{uid} (товара в номенклатуре НЕТ)"
    return f"{p.article or '—'} {p.size or '—'} {p.color or '—'}"


def _wire_key(db, account: PlatformAccount, uid: str, sent_sku: str,
              cache: dict) -> str:
    """Чем остаток РЕАЛЬНО адресован в теле запроса — не всегда `sent_sku`.

    У Kit и Ozon это одно и то же: `sent_sku` и есть ключ тела (variant_id,
    артикул). У WB — нет. Там `sent_sku` хранит БАРКОД, то есть то, чем
    рассылка адресовала позицию, а в теле уезжает chrtId, и окончательный
    выбор делает сам клиент, никому о нём не отчитываясь (см. `wb._chrt_id`).

    Отсюда весь смысл этой функции. Два товара 1С с РАЗНЫМИ баркодами могут
    вести на ОДИН размер карточки WB — тогда они пишут в одну и ту же ячейку
    и затирают друг друга, а по `sent_sku` столкновения не видно вовсе: ключи
    разные, раздел молчит. Молчащий раздел здесь хуже отсутствующего: по нему
    делают вывод «два товара на одну карточку ни при чём» и начинают искать
    беду в площадке.

    Правило выбора берём У КЛИЕНТА, а не повторяем здесь. Повтори мы его,
    раздел однажды обещал бы не то, что произойдёт при отправке.
    """
    if account.platform != Platform.wb:
        return sent_sku
    if uid not in cache:
        target = _resolve_push_target(db, uid, account.id)
        chrt = ""
        if target is not None:
            chrt = WbClient._chrt_id(StockPushItem(
                barcode=target[0], quantity=0, external_id=target[1]))
        cache[uid] = f"chrtId {chrt}" if chrt else ""
    return cache[uid] or sent_sku


def report(db, account: PlatformAccount, since, products_cache: dict,
           needle: str = "") -> None:
    rows = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account.id,
        DispatchQueueItem.created_at >= since,
    ).order_by(DispatchQueueItem.id).all()

    uids = {r.uid_1c for r in rows}
    missing = uids - set(products_cache)
    if missing:
        for p in db.query(Product).filter(Product.uid_1c.in_(list(missing))).all():
            products_cache[p.uid_1c] = p

    # Отбор по модели. Сужаем ЗДЕСЬ, до всех счётчиков: посчитай мы сводку по
    # кабинету целиком и покажи отфильтрованный список, числа перестали бы
    # относиться к строкам под ними — а по ним и решают.
    total_rows = len(rows)
    if needle:
        low = needle.lower()
        rows = [r for r in rows
                if low in f"{(products_cache.get(r.uid_1c).article or '') if products_cache.get(r.uid_1c) else ''} "
                          f"{(products_cache.get(r.uid_1c).name or '') if products_cache.get(r.uid_1c) else ''}".lower()]

    print("=" * 78)
    print(f"КАБИНЕТ {account.id}: {account.name} ({account.platform.value})"
          f"  активен={account.is_active}  рассылка={account.dispatch_enabled}")
    if needle:
        print(f"  отбор по «{needle}»: {len(rows)} записей из {total_rows}")
    if not rows:
        print("  за окно в очередь не попало НИ ОДНОЙ записи"
              + (" по этому отбору" if needle else ""))
        return

    # Ноль считаем по тому, что УШЛО, а не по тому, что поставили в очередь:
    # `quantity` — остаток на момент постановки, а итог даёт лестница в момент
    # отправки. Ставили 20, ушло 0 — это и есть случай, который ищут.
    zeros = [r for r in rows if r.sent_quantity == 0]
    nonzero = [r for r in rows if r.sent_quantity is not None and r.sent_quantity > 0]
    unsent = [r for r in rows if r.sent_quantity is None]

    print(f"  всего записей: {len(rows)}   "
          f"ушло НОЛЕЙ: {len(zeros)}   ушло непустых: {len(nonzero)}   "
          f"не отправлено: {len(unsent)}")

    by_reason = {}
    for r in zeros:
        by_reason.setdefault(r.reason, []).append(r)
    if by_reason:
        print("-" * 78)
        print("  ОТКУДА ВЗЯЛИСЬ НУЛИ (причина постановки в очередь):")
        for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            pairs = len({i.uid_1c for i in items})
            first, last = items[0].created_at, items[-1].created_at
            print(f"    {reason:22} {len(items):>5} записей, {pairs:>5} товаров"
                  f"   {first} … {last}")

    if nonzero:
        print("-" * 78)
        nz_reason = {}
        for r in nonzero:
            nz_reason[r.reason] = nz_reason.get(r.reason, 0) + 1
        print("  для сравнения — НЕПУСТЫЕ отправки за то же окно: "
              + ", ".join(f"{k}={v}" for k, v in sorted(nz_reason.items())))

    # Два товара на один ключ отправки. Ищем только среди УЖЕ отправленного:
    # `sent_sku` пишется только уехавшим.
    keys = {}
    wire_cache: dict[str, str] = {}
    for r in rows:
        if not r.sent_sku:
            continue
        keys.setdefault(_wire_key(db, account, r.uid_1c, r.sent_sku, wire_cache),
                        set()).add(r.uid_1c)
    clashes = {k: v for k, v in keys.items() if len(v) > 1}
    if clashes:
        print("-" * 78)
        print("  !! ОДИН КЛЮЧ — НЕСКОЛЬКО ТОВАРОВ (пишут по очереди, "
              "выигрывает последний):")
        for key, group in sorted(clashes.items()):
            print(f"    ключ {key}:")
            for uid in sorted(group):
                last = [r for r in rows if r.uid_1c == uid
                        and _wire_key(db, account, r.uid_1c, r.sent_sku or "",
                                      wire_cache) == key][-1]
                print(f"      {_label(products_cache, uid):40} "
                      f"последнее ушло {last.sent_quantity} в {last.created_at}"
                      f"  (адресовали {last.sent_sku})")

    print("-" * 78)
    print(f"  ПОСЛЕДНИЕ НУЛИ (до {SHOW_ROWS}, время UTC):")
    for r in zeros[-SHOW_ROWS:]:
        mark = " [ТЕСТ]" if getattr(r, "is_test", False) else ""
        err = f"  {r.last_error[:70]}" if r.last_error else ""
        print(f"    {r.created_at}  {_label(products_cache, r.uid_1c):38}"
              f"  ставили {r.quantity:>4} -> ушло {r.sent_quantity}"
              f"  {r.reason:20} {r.status.value if r.status else ''}"
              f" ключ={r.sent_sku or '—'}{mark}{err}")
    if len(zeros) > SHOW_ROWS:
        # Молчаливая обрезка — отдельный класс дефектов этого проекта: человек
        # считает, что видит всё, и не узнаёт про остальные.
        print(f"    (показаны последние {SHOW_ROWS} из {len(zeros)};"
              f" сузьте отбор третьим аргументом — артикулом или названием)")
    if not zeros:
        print("    нулей не было вовсе")

    # ПОЧЕМУ ноль, а не «отмечена ли пара». Это главный вопрос разбора, и
    # различие здесь ровно то же, что описано в `transmit`: ноль бывает двух
    # сортов, и путать их нельзя.
    #
    # Ноль от РАСЧЁТА — распродано, бронь или порог съели остаток — законное
    # сообщение площадке «не продавать». 26.09 на бою это стоило ложной тревоги
    # на 517 товаров: порог 6 при остатке 5 даёт ноль по построению, всё
    # работало правильно, а скрипт объявил «надо разбирать». Находка,
    # срабатывающая на норме, приучает пролистывать вывод целиком — и тогда
    # настоящую она уже не покажет.
    #
    # Ноль от ВЫКЛЮЧАТЕЛЯ (ступени 0-3) значит обратное: этой карточкой мы не
    # управляем. Вот его и разбирают.
    #
    # Причину спрашиваем у САМОЙ системы (`transmit.explain`), а не считаем
    # здесь: повтори мы лестницу у себя, она однажды разошлась бы с боевой, и
    # разбор объяснял бы не то, что произошло.
    zero_uids = {r.uid_1c for r in zeros}
    if zero_uids:
        settings = {s.uid_1c: s for s in db.query(SyncSetting).filter(
            SyncSetting.account_id == account.id,
            SyncSetting.uid_1c.in_(list(zero_uids))).all()}
        by_cause: dict = {}
        for uid in zero_uids:
            product = products_cache.get(uid)
            verdict = explain(product, settings.get(uid), account)
            if verdict.blocked:
                cause = f"ВЫКЛЮЧАТЕЛЬ: {verdict.reason}"
            elif verdict.quantity == 0:
                cause = f"законный ноль: {_why_empty(product)}"
            else:
                # Тогда ушёл ноль, а сегодня ушло бы число: состояние пары
                # изменилось ПОСЛЕ записи. Это не ошибка и не норма — это
                # сообщение о том, что смотреть надо не на сегодняшний день.
                cause = f"сейчас ушло бы {verdict.quantity} — состояние изменилось"
            by_cause.setdefault(cause, []).append(uid)

        print("-" * 78)
        print(f"  ПОЧЕМУ УШЁЛ НОЛЬ (по СЕГОДНЯШНЕМУ состоянию пары, "
              f"всего товаров {len(zero_uids)}):")
        for cause, uids in sorted(by_cause.items(), key=lambda kv: -len(kv[1])):
            print(f"    {len(uids):>5}  {cause}")
        blocked_n = sum(len(v) for c, v in by_cause.items()
                        if c.startswith("ВЫКЛЮЧАТЕЛЬ"))
        if blocked_n:
            print(f"\n    Разбирают только ВЫКЛЮЧАТЕЛЬ — здесь таких {blocked_n}.")
        else:
            print("\n    Ни одного нуля от выключателя: разбирать нечего.")
        print("    Законный ноль — это распродано, бронь или порог съели остаток;")
        print("    такой ноль площадке уходить ОБЯЗАН.")


def main() -> int:
    # Имена РАЗНЫЕ намеренно: кабинет и модель — два разных отбора, и общее имя
    # на оба уже стоило дефекта. Вторая присвойка затирала первую, `_accounts`
    # получал пустую строку, ilike '%%' находил ВСЕ кабинеты — и скрипт молча
    # отвечал не про тот кабинет, о котором спросили.
    cabinet = sys.argv[1].strip() if len(sys.argv) > 1 else "все"
    try:
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_HOURS
    except ValueError:
        print("второй аргумент — число часов")
        return 1
    # Третий аргумент — модель: артикул или кусок названия. Вопрос «кто ставил
    # ноль по 3030» без него требует глазами вычитывать список по всему
    # кабинету, где таких записей тысячи.
    needle = sys.argv[3].strip() if len(sys.argv) > 3 else ""
    since = now_utc() - timedelta(hours=hours)

    db = SessionLocal()
    try:
        accounts = _accounts(db, cabinet)
        if not accounts:
            have = db.query(PlatformAccount).order_by(PlatformAccount.id).all()
            print(f"кабинет «{cabinet}» не найден. Есть: "
                  + ", ".join(f"{a.id}:{a.name}" for a in have))
            return 1

        print(f"окно: последние {hours} ч (с {since} UTC)")
        cache: dict = {}
        for account in accounts:
            report(db, account, since, cache, needle)

        # Массовые действия печатаем ОДИН раз в конце и без привязки к кабинету:
        # кнопка отбора и залитый файл трогают сразу много пар и много кабинетов,
        # и связывать их надо по ВРЕМЕНИ с нулями выше.
        print("=" * 78)
        print(f"МАССОВЫЕ ДЕЙСТВИЯ ЗА ТО ЖЕ ОКНО (время UTC):")
        bulk = db.query(AuditLog).filter(
            AuditLog.created_at >= since,
            AuditLog.action.in_(["products_bulk", "products_bulk_import_excel",
                                 "recalc_started", "resend_all",
                                 "mapping_import_excel"]),
        ).order_by(AuditLog.id).all()
        for r in bulk:
            print(f"  {r.created_at}  {r.actor:12} {r.action:26} {(r.details or '')[:120]}")
        if not bulk:
            print("  пусто — массовых правок в этом окне не было")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
