import uuid
from datetime import datetime, date

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import (
    Product, Barcode, SyncSetting, PlatformAccount, ProcessedOrder, OrderProcessStatus,
    DispatchQueueItem, FtpTask, FtpTaskStatus, SyncAnomaly, TestLogEntry, TestLogLevel, User,
)
from app.workers.client_factory import build_client
from app.workers.credentials import CredentialsMissing
from app.workers.order_poller import (process_new_order, process_cancellation, process_confirmation,
                                      TEST_ORDER_PREFIX, open_test_out)
from app.workers.scheduler import PENDING_WAREHOUSE_NAME, SOLD_WAREHOUSE_NAME
from app.workers.dispatch import _resolve_push_target, _quantity_to_send
from app.transmit import explain
from app.workers.platform_clients.base import PlatformOrder, StockPushItem
from app.audit import log_action
from app.flash import set_flash, pop_flash

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")



def _new_test_order_id() -> str:
    return f"{TEST_ORDER_PREFIX}{uuid.uuid4().hex[:10]}"


def _log(db: Session, uid_1c: str, account_id: int, level: str, action: str, message: str):
    """Пишет строку в живой журнал страницы тестирования. Отдельно от
    log_action (общий аудит-лог админки) — этот журнал детальнее и живёт
    только на странице тестирования, для отладки прямо во время прогона."""
    db.add(TestLogEntry(uid_1c=uid_1c, account_id=account_id, level=level, action=action, message=message))


def _load_log(db: Session, uid_1c: str, account_id: int, limit: int = 100):
    return db.query(TestLogEntry).filter(
        TestLogEntry.uid_1c == uid_1c, TestLogEntry.account_id == account_id,
    ).order_by(TestLogEntry.created_at.desc()).limit(limit).all()


def _sku_send(product) -> int:
    """«Сейчас передаётся» на уровне SKU. Лестница — в app/transmit.py, один модуль
    на рассылку и интерфейс (пороги кабинетов применяются отдельно, по кабинету)."""
    return explain(product, None, None).quantity


def _real_processed_orders(db: Session, uid_1c: str) -> list:
    """Реальные (не синтетические TEST-) обработанные заказы товара по всем кабинетам —
    след прошлого бэкфилла/живого опроса. Именно их снимает «Сброс истории бэкфилла»."""
    return db.query(ProcessedOrder).filter(
        ProcessedOrder.uid_1c == uid_1c, ProcessedOrder.order_id.notlike(f"{TEST_ORDER_PREFIX}%"),
    ).order_by(ProcessedOrder.processed_at.asc()).all()


def _open_1c_tasks(db: Session, orders: list) -> int:
    """Сколько заданий 1С по этим заказам не завершены благополучно: пока 1С может
    их обработать (pending/sent) или пока непонятно, что с ними стало (timeout —
    ответа нет, failed — 1С ответила отказом), сбрасывать историю нельзя: в 1С
    появится документ без записи у нас либо останется неразобранный отказ."""
    n = 0
    for o in orders:
        n += db.query(FtpTask).filter(
            FtpTask.order_id == o.order_id, FtpTask.account_id == o.account_id,
            FtpTask.status.in_([FtpTaskStatus.pending, FtpTaskStatus.sent,
                                FtpTaskStatus.timeout, FtpTaskStatus.failed]),
            FtpTask.is_test.is_(False),
        ).count()
    return n


def _backfill_summary(db: Session, product) -> dict:
    if product is None:
        return {"orders": 0, "open_tasks": 0}
    orders = _real_processed_orders(db, product.uid_1c)
    return {"orders": len(orders), "open_tasks": _open_1c_tasks(db, orders)}


def _load_context(db: Session, uid_1c: str | None, account_id: int | None):
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first() if uid_1c else None
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first() if account_id else None

    barcode = None
    if product:
        barcode_row = db.query(Barcode).filter(Barcode.uid_1c == product.uid_1c).first()
        barcode = barcode_row.barcode if barcode_row else None

    setting = None
    if product and account:
        setting = db.query(SyncSetting).filter(
            SyncSetting.uid_1c == product.uid_1c, SyncSetting.account_id == account.id,
        ).first()

    test_orders = []
    if product and account:
        test_orders = db.query(ProcessedOrder).filter(
            ProcessedOrder.uid_1c == product.uid_1c, ProcessedOrder.account_id == account.id,
            ProcessedOrder.order_id.like(f"{TEST_ORDER_PREFIX}%"),
        ).order_by(ProcessedOrder.processed_at.desc()).all()

    return product, account, barcode, setting, test_orders


def _acc_int(account_id) -> int | None:
    """Разбор account_id из формы/URL: 'all' и пусто → None, иначе int."""
    if account_id in (None, "", "all"):
        return None
    try:
        return int(account_id)
    except (TypeError, ValueError):
        return None


def _selected_accounts(db: Session, account_id) -> list:
    """Список кабинетов по account_id: 'all' → все, иначе один (или пусто)."""
    if str(account_id) == "all":
        return db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()
    aid = _acc_int(account_id)
    if aid is None:
        return []
    a = db.query(PlatformAccount).filter(PlatformAccount.id == aid).first()
    return [a] if a else []


def _fetch_real_orders(db: Session, uid_1c: str, account_id, start_date: str):
    """Тянет с площадки реальные FBS-заказы товара с даты start_date по одному
    кабинету или ПО ВСЕМ (account_id == 'all'). Оставляет только заказы, чей
    баркод принадлежит выбранному товару. Ничего не меняет — только читает.
    Возвращает (список dict, текст_ошибки|None). Каждый dict: order_id, barcode,
    quantity, order_date, raw_status, already (идемпотентность по ProcessedOrder),
    account_id, account_name. При 'all' ошибка отдельного кабинета не прерывает
    остальные — собирается в предупреждение; если заказы есть, они всё равно
    возвращаются."""
    raw = (start_date or "").strip()
    if not raw:
        return [], "Укажите «Дату начала» в форме выбора вверху."
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return [], "Дата должна быть в формате ГГГГ-ММ-ДД."

    # Баркоды именно выбранного товара (пул размер-цвета). Фильтруем по
    # принадлежности напрямую — не через resolve_barcode, чтобы не плодить
    # побочных MappingConflict по чужим баркодам всего кабинета.
    product_barcodes = {b.barcode for b in db.query(Barcode).filter(Barcode.uid_1c == uid_1c).all()}
    if not product_barcodes:
        return [], "У товара нет баркодов — сначала сопоставьте его на «Мэппинг»."

    accounts = _selected_accounts(db, account_id)
    if not accounts:
        return [], "Кабинет не найден."

    rows = []
    warnings = []
    for account in accounts:
        try:
            client = build_client(db, account.id)
        except CredentialsMissing as e:
            warnings.append(f"{account.name}: нет ключей ({e})")
            continue
        try:
            orders = client.get_orders_since(d)
        except Exception as e:
            warnings.append(f"{account.name}: ошибка площадки ({type(e).__name__}: {e})")
            continue

        seen = set()
        for o in orders:
            if o.order_id in seen or o.barcode not in product_barcodes:
                continue
            seen.add(o.order_id)
            already = db.query(ProcessedOrder).filter(
                ProcessedOrder.account_id == account.id, ProcessedOrder.order_id == o.order_id,
            ).first() is not None
            rows.append({
                "order_id": o.order_id, "barcode": o.barcode, "quantity": o.quantity,
                "order_date": o.order_date, "raw_status": o.raw_status, "already": already,
                "account_id": account.id, "account_name": account.name,
            })
    rows.sort(key=lambda r: (r["order_date"] or date(1970, 1, 1), r["account_name"]))

    error = None
    if warnings and not rows:
        error = "; ".join(warnings)
    elif warnings:
        error = "Часть кабинетов недоступна: " + "; ".join(warnings)
    return rows, error


def _resolve_start(db: Session, uid_1c: str, start_date: str, persist: bool = False) -> str:
    """Единая дата старта задним числом = Product.broadcast_active_since.
    Введённая оператором дата побеждает, пустое поле — берём сохранённую.
    Возвращает строку ГГГГ-ММ-ДД (или пустую).

    `persist=False` (по умолчанию) — ТОЛЬКО ЧТЕНИЕ. Просмотр заказов с площадки
    обещает кнопкой и докстрингом, что ничего не меняет, а на деле молча
    сохранял дату старта товара — без сообщения и без записи в аудит. Сохраняем
    только там, где оператор явно что-то проводит (бэкфилл), и пишем об этом в
    журнал действий."""
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    eff = (start_date or "").strip()
    if eff:
        try:
            d = datetime.strptime(eff, "%Y-%m-%d").date()
            if persist and product is not None:
                product.broadcast_active_since = d
        except ValueError:
            pass  # некорректный формат отловит _fetch_real_orders ниже
    elif product is not None and product.broadcast_active_since:
        eff = product.broadcast_active_since.isoformat()
    return eff


@router.get("/testing", response_class=HTMLResponse)
def testing_page(
    request: Request, uid_1c: str = "", account_id: str = "", start_date: str = "",
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    accounts = db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()

    all_mode = account_id == "all"
    acc_int = _acc_int(account_id)
    product, account, barcode, setting, test_orders = _load_context(db, uid_1c or None, acc_int)

    # Единая дата старта задним числом = Product.broadcast_active_since: если в
    # запросе даты нет — подставляем сохранённую (общую для всех площадок и страниц).
    if not start_date and product and product.broadcast_active_since:
        start_date = product.broadcast_active_since.isoformat()

    log_entries = _load_log(db, uid_1c, acc_int) if (product and account) else []

    return templates.TemplateResponse(request, "testing.html", {
        "request": request, "current_user": user, "active_page": "testing",
        "accounts": accounts,
        "selected_uid": uid_1c, "selected_account_id": account_id, "start_date": start_date,
        "all_mode": all_mode,
        "product": product, "account": account, "barcode": barcode, "setting": setting,
        "test_orders": test_orders, "log_entries": log_entries,
        "current_send": _sku_send(product),
        # Остаток «глазами симуляции»: боевой минус открытые тестовые заказы.
        # Сам боевой остаток тест не трогает, поэтому показываем оба числа —
        # иначе оператор видит в журнале одно, а в карточке другое.
        "simulated_stock": (product.stock_on_hand - open_test_out(db, product.uid_1c)) if product else None,
        "backfill": _backfill_summary(db, product),
        "flash": pop_flash(request),
    })


@router.get("/testing/search-products", response_class=HTMLResponse)
def search_products(
    request: Request, q: str = "", account_id: str = "", start_date: str = "",
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """HTMX-поиск по СОПОСТАВЛЕННЫМ товарам (у которых есть баркод) — по
    артикулу или наименованию. Возвращает фрагмент со ссылками выбора."""
    q = (q or "").strip()
    results = []
    if q:
        like = f"%{q}%"
        results = (
            db.query(Product)
            .join(Barcode, Barcode.uid_1c == Product.uid_1c)
            .filter((Product.article.ilike(like)) | (Product.name.ilike(like)))
            .order_by(Product.name)
            .distinct()
            .limit(50)
            .all()
        )
    return templates.TemplateResponse(request, "testing_product_results.html", {
        "request": request, "results": results, "q": q,
        "account_id": account_id, "start_date": start_date,
    })


@router.get("/testing/log-rows", response_class=HTMLResponse)
def testing_log_rows(
    request: Request, uid_1c: str = "", account_id: str = "",
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """HTMX-фрагмент для живого опроса — только строки журнала, без
    перерисовки всей страницы. Опрашивается раз в пару секунд с клиента."""
    log_entries = _load_log(db, uid_1c, int(account_id)) if (uid_1c and account_id) else []
    return templates.TemplateResponse(request, "testing_log_rows.html", {"request": request, "log_entries": log_entries})


@router.post("/testing/push-stock")
def test_push_stock(
    request: Request, uid_1c: str = Form(...), account_id: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Шаг 1: реальная (но безопасная) отправка текущего остатка на площадку —
    подтверждает, что ключи и склад настроены верно. Использует ТОТ ЖЕ путь,
    что боевая рассылка: резолвинг идентификатора площадки (Ozon offer_id /
    Kit variant_id), вычет резерва и минимального порога, клампинг
    отрицательного остатка — иначе тест проверял бы не то, что уходит в бою."""
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    target = _resolve_push_target(db, uid_1c, account_id)

    if product is None or target is None:
        _log(db, uid_1c, account_id, TestLogLevel.warn, "push_stock", "У товара нет баркода — операция отменена.")
        db.commit()
        set_flash(request, "У товара нет баркода — сначала сопоставьте его на странице «Мэппинг».", "warn")
        return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)

    barcode, external_id, article = target
    quantity = _quantity_to_send(db, uid_1c, account_id, product.stock_on_hand)

    _log(db, uid_1c, account_id, TestLogLevel.info, "push_stock",
         f"Отправка: физический остаток {product.stock_on_hand}, к отправке {quantity} шт. "
         f"(после резерва/порога) по баркоду {barcode}...")

    try:
        client = build_client(db, account_id)
        account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
        result = client.push_stock(account.warehouse_id, [
            StockPushItem(barcode=barcode, quantity=quantity, external_id=external_id, article=article),
        ])
        ok = barcode in result.get("ok", [])
        log_action(db, user.username, "test_push_stock", f"{uid_1c} / кабинет #{account_id}: {result}")
        if ok:
            _log(db, uid_1c, account_id, TestLogLevel.good, "push_stock",
                 f"Успешно. Ответ площадки: ok={result.get('ok')}, errors={result.get('errors')}")
            set_flash(request, f"Остаток {quantity} шт. успешно отправлен в «{account.name}».", "good")
        else:
            _log(db, uid_1c, account_id, TestLogLevel.warn, "push_stock",
                 f"Площадка вернула ошибку: {result.get('errors')}")
            set_flash(request, f"Площадка вернула ошибку: {result.get('errors')}", "warn")
    except CredentialsMissing as e:
        _log(db, uid_1c, account_id, TestLogLevel.error, "push_stock", f"Нет ключей: {e}")
        set_flash(request, str(e), "warn")
    except Exception as e:
        # Откат по той же причине, что и в бэкфилле: на сломанной сессии
        # следующий же db.commit() ниже вылетел бы 500 вместо понятного сообщения.
        db.rollback()
        _log(db, uid_1c, account_id, TestLogLevel.error, "push_stock", f"Исключение: {type(e).__name__}: {e}")
        set_flash(request, f"Ошибка отправки: {e}", "warn")

    db.commit()
    return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)


@router.post("/testing/simulate-order")
def simulate_order(
    request: Request, uid_1c: str = Form(...), account_id: int = Form(...), quantity: int = Form(1),
    order_date: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Шаг 2: прогоняет СИНТЕТИЧЕСКИЙ заказ через process_new_order — ровно
    ту же функцию, что использует живой опрос заказов. Не требует реальной
    покупки, но проверяет весь наш код: сопоставление, списание, рассылку,
    задание в 1С. ID заказа с префиксом TEST- — чтобы никогда не спутать
    с настоящим заказом в отчётах."""
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    barcode_row = db.query(Barcode).filter(Barcode.uid_1c == uid_1c).first()

    if product is None or account is None or barcode_row is None:
        _log(db, uid_1c, account_id, TestLogLevel.warn, "simulate_order",
             "Не выбран товар, кабинет или у товара нет баркода — операция отменена.")
        db.commit()
        set_flash(request, "Не выбран товар, кабинет или у товара нет баркода.", "warn")
        return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)

    if quantity < 1:
        quantity = 1

    # Дата начала (старт задним числом): перемещение в 1С датируется этой датой.
    parsed_date = None
    raw_date = (order_date or "").strip()
    if raw_date:
        try:
            parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            set_flash(request, "Дата должна быть в формате ГГГГ-ММ-ДД — заказ не создан.", "warn")
            db.commit()
            return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)

    order = PlatformOrder(
        order_id=_new_test_order_id(), barcode=barcode_row.barcode, quantity=quantity, raw_status="test",
    )
    warehouse_pending = PENDING_WAREHOUSE_NAME.get(account.platform, "Ожидает")

    _log(db, uid_1c, account_id, TestLogLevel.info, "simulate_order",
         f"Симулирую заказ {order.order_id} на {quantity} шт. по баркоду {barcode_row.barcode}, "
         f"склад «Ожидает» → {warehouse_pending}"
         f"{', дата документа ' + raw_date if parsed_date else ''}...")

    result = process_new_order(db, order, account, warehouse_pending, is_test=True, order_date=parsed_date)
    log_action(db, user.username, "test_simulate_order",
               f"{uid_1c} / кабинет #{account_id}, qty={quantity}: {result}")

    level = TestLogLevel.good if result["status"] == "processed" else TestLogLevel.warn
    detail = (
        f"Статус: {result['status']}. uid_1c={result['uid_1c']}. Новый остаток: {result['new_stock']}. "
        f"Разослано в кабинеты: {result['dispatched_to']}. FtpTask id={result['ftp_task_id']} (is_test=True, "
        f"в реальный файл для 1С не попадёт). Аномалия создана: {result['anomaly_created']}. {result['detail']}"
    )
    _log(db, uid_1c, account_id, level, "simulate_order", detail)
    db.commit()

    message = (
        f"Заказ {order.order_id}: статус «{result['status']}». "
        f"{'Остаток стал ' + str(result['new_stock']) + '.' if result['new_stock'] is not None else ''} "
        f"{'Поставлено в очередь рассылки (не отправится — тестовая пометка): ' + str(len(result['dispatched_to'])) + ' кабинет(ов).' if result['dispatched_to'] else ''} "
        f"{'Тестовое задание в 1С создано (#' + str(result['ftp_task_id']) + '), в реальный файл для 1С не попадёт.' if result['ftp_task_id'] else ''}"
        f"{result['detail']}"
    )
    set_flash(request, message, "good" if result["status"] == "processed" else "warn")

    return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)


@router.post("/testing/simulate-confirm")
def simulate_confirm(
    request: Request, uid_1c: str = Form(...), account_id: int = Form(...), order_id: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Шаг подтверждения: тот же process_confirmation, что и живой опрос —
    перемещение «<Площадка>.Ожидает» → «Склад <Площадка>». Остаток не меняет.
    Помечает задание в 1С is_test=True (в реальный файл не попадёт)."""
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    record = db.query(ProcessedOrder).filter(
        ProcessedOrder.order_id == order_id, ProcessedOrder.account_id == account_id,
        ProcessedOrder.status == OrderProcessStatus.processed,
    ).first()

    if account is None or record is None:
        _log(db, uid_1c, account_id, TestLogLevel.warn, "simulate_confirm",
             f"Заказ {order_id} не найден или уже подтверждён/отменён — операция отменена.")
        db.commit()
        set_flash(request, "Заказ не найден или уже подтверждён/отменён.", "warn")
        return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)

    warehouse_pending = PENDING_WAREHOUSE_NAME.get(account.platform, "Ожидает")
    warehouse_sold = SOLD_WAREHOUSE_NAME.get(account.platform, f"Склад {account.platform.value.upper()}")

    _log(db, uid_1c, account_id, TestLogLevel.info, "simulate_confirm",
         f"Симулирую подтверждение {order_id}: перемещение {warehouse_pending} → {warehouse_sold}...")

    result = process_confirmation(db, record, account, warehouse_pending, warehouse_sold, is_test=True)
    log_action(db, user.username, "test_simulate_confirm", f"{order_id}: {result}")

    _log(db, uid_1c, account_id, TestLogLevel.good, "simulate_confirm",
         f"Статус: {result['status']}. Перемещение {warehouse_pending} → {warehouse_sold}. "
         f"FtpTask id={result['ftp_task_id']} (is_test=True). Остаток не изменён.")
    db.commit()

    set_flash(request, f"Подтверждение {order_id}: перемещение на «{warehouse_sold}» (тест).", "good")
    return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)


@router.post("/testing/simulate-cancel")
def simulate_cancel(
    request: Request, uid_1c: str = Form(...), account_id: int = Form(...), order_id: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Шаг 3: реверс тестового заказа — проверяет обратный ход (раздел 5)."""
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    record = db.query(ProcessedOrder).filter(
        ProcessedOrder.order_id == order_id, ProcessedOrder.account_id == account_id,
        ProcessedOrder.status == OrderProcessStatus.processed,
    ).first()

    if account is None or record is None:
        _log(db, uid_1c, account_id, TestLogLevel.warn, "simulate_cancel",
             f"Заказ {order_id} не найден или уже отменён — операция отменена.")
        db.commit()
        set_flash(request, "Заказ не найден или уже отменён.", "warn")
        return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)

    _log(db, uid_1c, account_id, TestLogLevel.info, "simulate_cancel", f"Симулирую отмену заказа {order_id}...")

    cancelled_order = PlatformOrder(order_id=order_id, barcode="", quantity=0, raw_status="test_cancel", is_cancellation=True)
    result = process_cancellation(db, cancelled_order, record, account, is_test=True)
    log_action(db, user.username, "test_simulate_cancel", f"{order_id}: {result}")

    _log(db, uid_1c, account_id, TestLogLevel.good, "simulate_cancel",
         f"Статус: {result['status']}. Возвращено {result['return_quantity']} шт., остаток стал {result['new_stock']}. "
         f"Разослано в кабинеты: {result['dispatched_to']}. FtpTask id={result['ftp_task_id']} (is_test=True).")
    db.commit()

    set_flash(request, f"Отмена {order_id}: возвращено {result['return_quantity']} шт., остаток стал {result['new_stock']}.", "good")
    return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)


@router.post("/testing/cleanup")
def cleanup_test_data(
    request: Request, uid_1c: str = Form(...), account_id: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Полная очистка тестовых следов по этому товару+кабинету.

    Все побочные записи от симуляции (DispatchQueueItem, FtpTask, SyncAnomaly)
    создаются с is_test=True — это единственное, что отличает их от боевых,
    и dispatch.py/ftp_channel.py/anomalies.py уже отфильтровывают такие
    записи от реальной обработки (см. models.py). Поэтому здесь их можно
    удалять точно по флагу, не рискуя задеть настоящие данные — раньше,
    до появления этого флага, DispatchQueueItem вообще не трогали при
    очистке именно из-за риска зацепить чужую запись."""

    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    test_orders = db.query(ProcessedOrder).filter(
        ProcessedOrder.uid_1c == uid_1c, ProcessedOrder.account_id == account_id,
        ProcessedOrder.order_id.like(f"{TEST_ORDER_PREFIX}%"),
    ).all()

    _log(db, uid_1c, account_id, TestLogLevel.info, "cleanup",
         f"Очистка запущена. Найдено тестовых заказов по этому кабинету: {len(test_orders)}.")

    # Восстанавливать остаток больше НЕ НУЖНО и нельзя: симуляция его не трогает
    # (см. order_poller.process_new_order). Раньше тест списывал боевой остаток, а
    # очистка возвращала его обратно — если оставить только возврат, каждая очистка
    # молча ДОБАВЛЯЛА бы товар на склад.

    # Тестовые записи в очереди рассылки никогда не уходили на площадку
    # по-настоящему (is_test исключает их в dispatch.py) — удаляем точно
    # по флагу и по товару (фан-аут симуляции мог затронуть другие кабинеты).
    deleted_dispatch = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.uid_1c == uid_1c, DispatchQueueItem.is_test.is_(True),
    ).delete(synchronize_session=False)

    if test_orders and product is not None:
        # Реальным кабинетам с включённой синхронизацией шлём текущий боевой
        # остаток: тестовые значения до них не доходили (is_test исключает записи
        # в dispatch.py), но лишняя сверка с реальностью после теста не мешает.
        enabled_settings = db.query(SyncSetting).filter(
            SyncSetting.uid_1c == uid_1c, SyncSetting.enabled.is_(True),
        ).all()
        for setting in enabled_settings:
            db.add(DispatchQueueItem(
                uid_1c=uid_1c, account_id=setting.account_id,
                quantity=product.stock_on_hand, reason="test_cleanup",
            ))

    order_ids = [o.order_id for o in test_orders]
    deleted_ftp = deleted_anomaly = 0
    if order_ids:
        deleted_ftp = db.query(FtpTask).filter(FtpTask.order_id.in_(order_ids)).delete(synchronize_session=False)
        deleted_anomaly = db.query(SyncAnomaly).filter(SyncAnomaly.order_id.in_(order_ids)).delete(synchronize_session=False)
        db.query(ProcessedOrder).filter(ProcessedOrder.order_id.in_(order_ids)).delete(synchronize_session=False)

    _log(db, uid_1c, account_id, TestLogLevel.good, "cleanup",
         f"Готово. Боевой остаток симуляция не меняла, восстанавливать нечего "
         f"({product.stock_on_hand if product is not None else '—'} шт). Удалено: заказов {len(order_ids)}, "
         f"записей в очереди рассылки {deleted_dispatch}, заданий 1С {deleted_ftp}, аномалий {deleted_anomaly}.")

    log_action(db, user.username, "test_cleanup",
               f"{uid_1c} / кабинет #{account_id}: удалено тестовых заказов {len(order_ids)}")
    db.commit()

    set_flash(request, f"Тестовые данные очищены: заказов {len(order_ids)}. "
                       f"Боевой остаток симуляция не меняла.", "good")
    return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)


@router.post("/testing/load-real-orders", response_class=HTMLResponse)
def load_real_orders(
    request: Request, uid_1c: str = Form(...), account_id: str = Form(...), start_date: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Тянет с площадки реальные FBS-заказы по выбранному товару с указанной
    даты (по одному кабинету или по всем — account_id='all') и показывает их
    таблицей — БЕЗ каких-либо изменений в 1С, остатках или на площадках.
    Только просмотр перед бэкфиллом (кнопка «Провести»)."""
    accounts = db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()
    all_mode = account_id == "all"
    acc_int = _acc_int(account_id)
    product, account, barcode, setting, test_orders = _load_context(db, uid_1c or None, acc_int)
    log_entries = _load_log(db, uid_1c, acc_int) if (product and account) else []

    start_date = _resolve_start(db, uid_1c, start_date)
    real_orders, error = _fetch_real_orders(db, uid_1c, account_id, start_date)
    db.commit()

    return templates.TemplateResponse(request, "testing.html", {
        "request": request, "current_user": user, "active_page": "testing",
        "accounts": accounts,
        "selected_uid": uid_1c, "selected_account_id": account_id, "start_date": start_date,
        "all_mode": all_mode,
        "product": product, "account": account, "barcode": barcode, "setting": setting,
        "test_orders": test_orders, "log_entries": log_entries,
        "real_orders": real_orders, "real_orders_error": error,
        "current_send": _sku_send(product),
        "backfill": _backfill_summary(db, product),
        "flash": pop_flash(request),
    })


@router.post("/testing/backfill")
def backfill_real_orders(
    request: Request, uid_1c: str = Form(...), account_id: str = Form(...), start_date: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Бэкфилл «старта задним числом»: проводит перемещения по реальным
    FBS-заказам товара с даты start_date — по одному кабинету или ПО ВСЕМ
    (account_id='all'). Полный боевой путь process_new_order (списание остатка,
    рассылка на кабинеты, документ в 1С), is_test=False — в 1С эти отгрузки
    ещё не проведены, бэкфилл их и создаёт. Идемпотентность по ProcessedOrder
    (кабинет+order_id) не даёт провести заказ дважды. Каждый заказ проводится
    под своим кабинетом, дата документа = реальная дата заказа."""
    product_before = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    saved_before = product_before.broadcast_active_since if product_before else None
    start_date = _resolve_start(db, uid_1c, start_date, persist=True)
    if product_before is not None and product_before.broadcast_active_since != saved_before:
        log_action(db, user.username, "active_since_changed",
                   f"{uid_1c} -> {product_before.broadcast_active_since} (бэкфилл)")
    redirect = RedirectResponse(
        f"/testing?uid_1c={uid_1c}&account_id={account_id}&start_date={start_date}", status_code=303,
    )

    rows, error = _fetch_real_orders(db, uid_1c, account_id, start_date)
    if not rows:
        set_flash(request, error or "Заказов по этому товару с указанной даты не найдено.", "warn")
        db.commit()
        return redirect

    acc_cache: dict[int, object] = {}

    def _acc(aid: int):
        if aid not in acc_cache:
            acc_cache[aid] = db.query(PlatformAccount).filter(PlatformAccount.id == aid).first()
        return acc_cache[aid]

    done = skipped = failed = 0
    for r in rows:
        if r["already"]:
            skipped += 1
            continue
        account = _acc(r["account_id"])
        if account is None:
            failed += 1
            continue
        warehouse_pending = PENDING_WAREHOUSE_NAME.get(account.platform, "Ожидает")
        order = PlatformOrder(
            order_id=r["order_id"], barcode=r["barcode"], quantity=r["quantity"],
            raw_status=r["raw_status"], order_date=r["order_date"],
        )
        try:
            result = process_new_order(db, order, account, warehouse_pending,
                                       is_test=False, order_date=r["order_date"], respect_enabled=False)
            if result["status"] == "processed":
                done += 1
            elif result["status"] == "already_processed":
                skipped += 1
            else:
                failed += 1
            _log(db, uid_1c, r["account_id"], TestLogLevel.info, "backfill",
                 f"Заказ {r['order_id']} ({r['order_date']}, {r['account_name']}): {result['status']}. "
                 f"FtpTask={result['ftp_task_id']}, новый остаток={result['new_stock']}.")
            db.commit()      # строка журнала должна пережить сбой на следующем заказе
        except Exception as e:
            # Без отката сессия остаётся сломанной после неудачного commit внутри
            # process_new_order, и СЛЕДУЮЩЕЕ же обращение к базе (хоть запись в
            # журнал) вылетает наружу: часть заказов проведена, оператор видит 500
            # и, скорее всего, повторяет прогон — уже по частично проведённым
            # данным. Откатываем и продолжаем с остальными заказами.
            db.rollback()
            failed += 1
            _log(db, uid_1c, r["account_id"], TestLogLevel.error, "backfill",
                 f"Заказ {r['order_id']} ({r['account_name']}): {type(e).__name__}: {e}")
            db.commit()

    db.commit()
    log_action(db, user.username, "test_backfill",
               f"{uid_1c} / кабинет {account_id} с {start_date}: проведено {done}, пропущено {skipped}, ошибок {failed}")
    msg = f"Бэкфилл задним числом: проведено {done}, пропущено (уже были) {skipped}, ошибок {failed}."
    if error:
        msg += f" ⚠ {error}"
    set_flash(request, msg, "good" if failed == 0 else "warn")
    return redirect


@router.post("/testing/reset-backfill")
def reset_backfill(
    request: Request, uid_1c: str = Form(...), account_id: str = Form(""),
    confirm_deleted_in_1c: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Сброс истории бэкфилла по товару (все кабинеты): удаляет реальные записи
    прошлого прогона — ProcessedOrder (кроме синтетических TEST-, их снимает
    «Очистить тестовые данные»), задания 1С и аномалии по этим заказам, всю очередь
    рассылки и живой журнал товара. Нужен, чтобы провести «старт задним числом»
    заново после того, как оператор удалил документы перемещения в 1С: без сброса
    идемпотентность по ProcessedOrder пометит все заказы «уже проведён».

    Остаток stock_on_hand НЕ трогаем: он подтянется сверкой с 1С (раз в час) —
    восстанавливать вручную нельзя, т.к. сверка могла уже учесть удаление
    документов, и получилось бы задвоение. Отказываемся, пока по этим заказам есть
    незавершённые задания 1С (pending/sent): 1С ещё может их провести."""
    redirect = RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    if product is None:
        set_flash(request, "Товар не найден.", "warn")
        return redirect
    if not confirm_deleted_in_1c:
        set_flash(request, "Сброс не выполнен: подтвердите галочкой, что документы перемещения в 1С удалены.", "warn")
        return redirect

    orders = _real_processed_orders(db, uid_1c)
    if not orders:
        set_flash(request, "Истории бэкфилла по этому товару нет — сбрасывать нечего.", "warn")
        return redirect
    open_tasks = _open_1c_tasks(db, orders)
    if open_tasks:
        set_flash(request, f"Сброс не выполнен: по этим заказам ещё {open_tasks} незавершённых заданий 1С "
                           f"(pending/sent). Дождитесь их обработки (1–2 цикла обмена) и повторите.", "warn")
        return redirect

    deleted_ftp = deleted_anomaly = 0
    for o in orders:
        deleted_ftp += db.query(FtpTask).filter(
            FtpTask.order_id == o.order_id, FtpTask.account_id == o.account_id, FtpTask.is_test.is_(False),
        ).delete(synchronize_session=False)
        deleted_anomaly += db.query(SyncAnomaly).filter(
            SyncAnomaly.order_id == o.order_id, SyncAnomaly.account_id == o.account_id,
        ).delete(synchronize_session=False)
    order_pks = [o.id for o in orders]
    deleted_orders = db.query(ProcessedOrder).filter(ProcessedOrder.id.in_(order_pks)).delete(synchronize_session=False)
    deleted_dispatch = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == uid_1c).delete(synchronize_session=False)
    deleted_log = db.query(TestLogEntry).filter(TestLogEntry.uid_1c == uid_1c).delete(synchronize_session=False)

    # Площадкам — актуальное значение заново (dispatch пересчитает от текущего
    # остатка/порога в момент отправки).
    targets = []
    for s in db.query(SyncSetting).filter(SyncSetting.uid_1c == uid_1c, SyncSetting.enabled.is_(True)).all():
        db.add(DispatchQueueItem(uid_1c=uid_1c, account_id=s.account_id,
                                 quantity=product.stock_on_hand, reason="backfill_reset"))
        targets.append(s.account_id)

    msg = (f"История бэкфилла сброшена: заказов {deleted_orders}, заданий 1С {deleted_ftp}, аномалий {deleted_anomaly}, "
           f"записей рассылки {deleted_dispatch}, записей журнала {deleted_log}. Остаток ЦС не менялся "
           f"({product.stock_on_hand}) — сверьте его с 1С или дождитесь сверки перед новым прогоном.")
    acc_int = _acc_int(account_id)
    if acc_int is not None:
        _log(db, uid_1c, acc_int, TestLogLevel.good, "reset_backfill", f"{msg} Рассылка на кабинеты: {targets}.")
    log_action(db, user.username, "test_reset_backfill", f"{uid_1c}: {msg} targets={targets}")
    db.commit()
    set_flash(request, msg, "good")
    return redirect
