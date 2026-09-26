"""Кто отправлял НУЛИ на кабинет и почему — только чтение.

Запуск (окно по умолчанию — двое суток):

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_zero_sends.py КИТ
    ... probe_zero_sends.py КИТ 6          # только за последние 6 часов
    ... probe_zero_sends.py все 24         # по всем кабинетам сразу

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
товар, и каждая по-своему права.

Ничего не меняет и не коммитит.
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import (AuditLog, DispatchQueueItem, PlatformAccount,  # noqa: E402
                        Product, SyncSetting)
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


def _label(products: dict, uid: str) -> str:
    p = products.get(uid)
    if p is None:
        return f"{uid} (товара в номенклатуре НЕТ)"
    return f"{p.article or '—'} {p.size or '—'} {p.color or '—'}"


def report(db, account: PlatformAccount, since, products_cache: dict) -> None:
    rows = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account.id,
        DispatchQueueItem.created_at >= since,
    ).order_by(DispatchQueueItem.id).all()

    uids = {r.uid_1c for r in rows}
    missing = uids - set(products_cache)
    if missing:
        for p in db.query(Product).filter(Product.uid_1c.in_(list(missing))).all():
            products_cache[p.uid_1c] = p

    print("=" * 78)
    print(f"КАБИНЕТ {account.id}: {account.name} ({account.platform.value})"
          f"  активен={account.is_active}  рассылка={account.dispatch_enabled}")
    if not rows:
        print("  за окно в очередь не попало НИ ОДНОЙ записи")
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
    # `sent_sku` пишется только уехавшим, и по нему видно, чем адресовали.
    keys = {}
    for r in rows:
        if r.sent_sku:
            keys.setdefault(r.sent_sku, set()).add(r.uid_1c)
    clashes = {k: v for k, v in keys.items() if len(v) > 1}
    if clashes:
        print("-" * 78)
        print("  !! ОДИН КЛЮЧ — НЕСКОЛЬКО ТОВАРОВ (пишут по очереди, "
              "выигрывает последний):")
        for key, group in sorted(clashes.items()):
            print(f"    ключ {key}:")
            for uid in sorted(group):
                last = [r for r in rows if r.uid_1c == uid and r.sent_sku == key][-1]
                print(f"      {_label(products_cache, uid):40} "
                      f"последнее ушло {last.sent_quantity} в {last.created_at}")

    print("-" * 78)
    print(f"  ПОСЛЕДНИЕ НУЛИ (до {SHOW_ROWS}, время UTC):")
    for r in zeros[-SHOW_ROWS:]:
        mark = " [ТЕСТ]" if getattr(r, "is_test", False) else ""
        err = f"  {r.last_error[:70]}" if r.last_error else ""
        print(f"    {r.created_at}  {_label(products_cache, r.uid_1c):38}"
              f"  ставили {r.quantity:>4} -> ушло {r.sent_quantity}"
              f"  {r.reason:20} {r.status.value if r.status else ''}"
              f" ключ={r.sent_sku or '—'}{mark}{err}")
    if not zeros:
        print("    нулей не было вовсе")

    # Отмечена ли пара СЕЙЧАС — разный ответ меняет разбор целиком. Ноль по
    # снятой паре это отзыв, то есть работа человека; ноль по ОТМЕЧЕННОЙ и
    # включённой — расчёт или сбой, и вот это надо разбирать.
    zero_uids = {r.uid_1c for r in zeros}
    if zero_uids:
        marked = {s.uid_1c for s in db.query(SyncSetting).filter(
            SyncSetting.account_id == account.id,
            SyncSetting.uid_1c.in_(list(zero_uids)),
            SyncSetting.enabled.is_(True)).all()}
        on = {u for u in zero_uids
              if products_cache.get(u) is not None
              and products_cache[u].broadcast_enabled}
        both = marked & on
        print("-" * 78)
        print(f"  из {len(zero_uids)} обнулённых товаров ПРЯМО СЕЙЧАС: "
              f"кабинет отмечен у {len(marked)}, трансляция включена у {len(on)}, "
              f"и то и другое — у {len(both)}")
        if both:
            print("    (по ним ноль ушёл не отзывом — это и есть то, "
                  "что надо разбирать)")


def main() -> int:
    needle = sys.argv[1].strip() if len(sys.argv) > 1 else "все"
    try:
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_HOURS
    except ValueError:
        print("второй аргумент — число часов")
        return 1
    since = now_utc() - timedelta(hours=hours)

    db = SessionLocal()
    try:
        accounts = _accounts(db, needle)
        if not accounts:
            have = db.query(PlatformAccount).order_by(PlatformAccount.id).all()
            print(f"кабинет «{needle}» не найден. Есть: "
                  + ", ".join(f"{a.id}:{a.name}" for a in have))
            return 1

        print(f"окно: последние {hours} ч (с {since} UTC)")
        cache: dict = {}
        for account in accounts:
            report(db, account, since, cache)

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
