"""Сверка отправленного с тем, что площадка действительно держит.

Зачем. Успешный ответ на отправку не означает, что наше число на площадке
осталось. 19.09.2026 на боевом кабинете WB это вскрылось в чистом виде: мы
отправили 68/40/110, получили `204`, значения применились — и через три минуты
там снова лежало чужое (0/36/108). В кабинет писала ещё одна система, та, с
которой идёт переход. Отправка о таком не знает и знать не может: она видит
только момент записи.

Поэтому спрашиваем площадку ОТДЕЛЬНО и позже: что у тебя сейчас по этим sku.
Расхождение с `sent_quantity` означает одно из двух, и оба стоят внимания:
  * кто-то пишет поверх нас (перетирание — то, что нашли 19.09);
  * площадка приняла отправку не так, как мы поняли из её ответа.

Направление расхождения решает, насколько это срочно. Площадка держит МЕНЬШЕ —
недоотправка, теряются продажи. БОЛЬШЕ — она продаёт то, чего нет, и это уже
оверселл; такую находку отчёт поднимает до критичной.

Почему отдельный воркер, а не проверка внутри отчёта. Отчёт о расхождениях
только читает базу — на этом он и держится: его не страшно запускать, он не
ходит наружу и не зависит от доступности площадок. Запрос в API площадки внутри
отчёта сломал бы оба свойства (лимиты запросов, сетевые сбои, время ответа).
Поэтому наружу ходит воркер и кладёт ответ в те же строки очереди, а отчёт
читает уже сохранённое.

Сверка НИЧЕГО НЕ ЧИНИТ: не переотправляет, не правит остаток, не трогает
очередь кроме своих двух колонок. Автоматическая переотправка при живой второй
системе превратилась бы в гонку двух писателей — они бы перебивали друг друга
до бесконечности, и остаток на площадке скакал бы тем чаще, чем быстрее мы
реагируем. Решение, что делать с расхождением, принимает человек.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import DispatchQueueItem, DispatchStatus, PlatformAccount
from app.timeutils import now_utc

logger = logging.getLogger("sync_worker")

# Сколько ждать после отправки, прежде чем спрашивать площадку. Слишком рано —
# поймаем ещё не применённое значение и объявим расхождением; слишком поздно —
# пропустим момент. Пять минут: на живом WB наше число применялось за секунды, а
# чужая запись пришла через три минуты.
SETTLE_DELAY = timedelta(minutes=5)

# Насколько назад смотрим. Смысл сверки в свежих отправках: по строке недельной
# давности расхождение ничего не говорит — с тех пор остаток менялся десять раз.
LOOKBACK = timedelta(hours=24)

# Сколько позиций проверяем за один прогон на кабинет. Ограничение не наше, а
# уважение к лимитам площадки: запрос остатков идёт пачками, и выгребать весь
# каталог каждые полчаса незачем.
MAX_PER_ACCOUNT = 500


def rows_to_verify(db: Session, account_id: int) -> list[DispatchQueueItem]:
    """Отправленные записи, по которым имеет смысл спросить площадку.

    Берём ПОСЛЕДНЮЮ отправку по каждому товару, а не все подряд: если за сутки
    по товару ушло пять чисел, площадка обязана держать пятое, и сравнивать с
    первыми четырьмя бессмысленно — они устарели законно.
    """
    now = now_utc()
    rows = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account_id,
        DispatchQueueItem.status == DispatchStatus.sent,
        DispatchQueueItem.is_test.is_(False),
        DispatchQueueItem.sent_at.isnot(None),
        DispatchQueueItem.sent_sku.isnot(None),
        DispatchQueueItem.sent_quantity.isnot(None),
        DispatchQueueItem.sent_at <= now - SETTLE_DELAY,
        DispatchQueueItem.sent_at >= now - LOOKBACK,
    ).order_by(DispatchQueueItem.sent_at.desc()).all()

    latest: dict[str, DispatchQueueItem] = {}
    for row in rows:
        latest.setdefault(row.uid_1c, row)       # rows уже по убыванию времени
    return list(latest.values())[:MAX_PER_ACCOUNT]


def verify_account(db: Session, client, account: PlatformAccount) -> dict:
    """Спросить площадку по одному кабинету и записать ответ в строки очереди."""
    stats = {"checked": 0, "match": 0, "diverged": 0, "unknown_sku": 0, "skipped": 0}

    rows = rows_to_verify(db, account.id)
    if not rows:
        return stats

    by_sku: dict[str, list[DispatchQueueItem]] = {}
    for row in rows:
        by_sku.setdefault(row.sent_sku, []).append(row)

    held = client.get_stocks(account.warehouse_id, list(by_sku))
    if held is None:
        # Площадка не умеет отдавать остатки назад или не ответила. Это не
        # расхождение и не совпадение — это отсутствие проверки, и пометить
        # строки чем-либо значило бы соврать.
        stats["skipped"] = len(rows)
        return stats

    now = now_utc()
    for sku, items in by_sku.items():
        for item in items:
            item.verified_at = now
            stats["checked"] += 1
            if sku not in held:
                # Площадка такого sku на этом складе не знает. Количество
                # оставляем пустым: ноль здесь означал бы «карточка есть и она
                # пуста», а это другое утверждение.
                item.verified_quantity = None
                stats["unknown_sku"] += 1
                continue
            item.verified_quantity = held[sku]
            if held[sku] == item.sent_quantity:
                stats["match"] += 1
            else:
                stats["diverged"] += 1
    db.commit()
    return stats


def verify_all(db: Session, build_client, accounts: list[PlatformAccount]) -> dict:
    """Пройти по кабинетам. Сбой одного не должен уносить остальные: площадки
    независимы, и молчание WB — не повод не проверить Kit."""
    total = {"checked": 0, "match": 0, "diverged": 0, "unknown_sku": 0,
             "skipped": 0, "errors": 0}
    for account in accounts:
        try:
            client = build_client(db, account.id)
            stats = verify_account(db, client, account)
        except Exception as e:                       # noqa: BLE001 — см. docstring
            total["errors"] += 1
            logger.warning("сверка остатков, кабинет «%s»: %s: %s",
                           account.name, type(e).__name__, e)
            continue
        for key, value in stats.items():
            total[key] += value
    return total
