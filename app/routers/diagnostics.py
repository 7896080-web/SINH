import collections
from datetime import datetime, timedelta
from app.timeutils import now_utc

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.models import (
    PlatformAccount, WorkerHeartbeat, DispatchQueueItem, DispatchStatus,
    Barcode, FtpTask, FtpTaskStatus, MappingConflict, Product, ReconciliationLog,
    SyncAnomaly, AnomalyStatus, User,
)
from app.workers.ftp_channel import (MAX_REPOSTS, repost_enabled, resolve_stuck_task,
                                     tasks_needing_review)
from app.workers.client_factory import build_client
from app.workers.credentials import CredentialsMissing
from app.workers.order_poller import poll_new_orders, poll_cancellations
from app.workers.catalog_sync import load_platform_catalog
from app.routers.platform_matching import clear_clusters_cache
from app.workers.scheduler import PENDING_WAREHOUSE_NAME
from app.audit import log_action
from app.report import (CRITICAL as REPORT_CRITICAL, collect_findings,
                        current_dispatch_errors)
from app.transmit import enqueue_resend_all
from app.flash import set_flash, pop_flash

router = APIRouter()
templates = shared_templates

# Насколько старым должно быть расхождение сверки, чтобы кнопка его закрыла.
# Сутки — ровно то окно, по которому отчёт показывает свежие: закрыть можно
# только то, что отчёт уже не считает находкой, иначе команда гасила бы сигнал.
CLOSE_RECONCILIATION_OLDER_THAN = timedelta(hours=24)
# По сколько строк за раз и сколько порций за одно нажатие. Те же соображения,
# что у `retention.CHUNK`: длинная транзакция блокирует запись всем остальным.
CLOSE_RECONCILIATION_CHUNK = 500
CLOSE_RECONCILIATION_MAX_CHUNKS = 200


def _queue_counts(db: Session, account_id: int, errors: int | None = None) -> dict:
    dispatch_pending = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account_id, DispatchQueueItem.status == DispatchStatus.pending,
        DispatchQueueItem.is_test.is_(False),
    ).count()
    # Считаем ТО ЖЕ, что показывает отчёт: последнюю запись пары товар+кабинет.
    # Иначе счётчик набирает мёртвые строки от давно починенных дефектов и
    # спорит с отчётом — на бою 20.09 он говорил «751» против «1 запись».
    #
    # Число приходит готовым: считать его здесь значило бы проходить всю очередь
    # заново на КАЖДЫЙ кабинет, а их пять.
    dispatch_errors = (errors if errors is not None
                       else len(current_dispatch_errors(db, account_id)))
    # Записи, которые уже сорвались и ждут следующей попытки: сама по себе это не
    # ошибка (площадка отвечает не всегда), но растущее число — повод посмотреть
    # в last_error, пока попытки не исчерпались и запись не стала ошибкой.
    dispatch_retry = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account_id, DispatchQueueItem.status == DispatchStatus.pending,
        DispatchQueueItem.attempts > 0, DispatchQueueItem.is_test.is_(False),
    ).count()
    ftp_pending = db.query(FtpTask).filter(
        FtpTask.account_id == account_id, FtpTask.status.in_([FtpTaskStatus.pending, FtpTaskStatus.sent]),
        FtpTask.is_test.is_(False),
    ).count()
    ftp_timeout = db.query(FtpTask).filter(
        FtpTask.account_id == account_id, FtpTask.status == FtpTaskStatus.timeout,
        FtpTask.is_test.is_(False),
    ).count()
    # 1С ответила ERROR: документа в её базе НЕТ, хотя товар мы уже списали.
    # Раньше такое задание закрывалось как успешное и не попадало никуда.
    ftp_failed = db.query(FtpTask).filter(
        FtpTask.account_id == account_id, FtpTask.status == FtpTaskStatus.failed,
        FtpTask.is_test.is_(False),
    ).count()
    return {
        "dispatch_pending": dispatch_pending, "dispatch_errors": dispatch_errors,
        "dispatch_retry": dispatch_retry,
        "ftp_pending": ftp_pending, "ftp_timeout": ftp_timeout, "ftp_failed": ftp_failed,
    }


def _heartbeat_for(db: Session, worker_name: str):
    return db.query(WorkerHeartbeat).filter(WorkerHeartbeat.worker_name == worker_name).first()


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accounts = db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()

    # Один проход по очереди на всю страницу, а не на каждый кабинет.
    errors_by_account = collections.Counter(
        r.account_id for r in current_dispatch_errors(db))

    account_rows = []
    for account in accounts:
        hb = _heartbeat_for(db, f"poll_orders_account_{account.id}")
        account_rows.append({
            "account": account,
            "queue": _queue_counts(db, account.id, errors_by_account[account.id]),
            "heartbeat": hb,
        })

    # `reconciliation_applied` — рядом с `reconciliation` намеренно: первое говорит,
    # когда сверка ЗАПУСКАЛАСЬ, второе — когда она в последний раз реально применила
    # выгрузку 1С. Разъехавшиеся времена в этих двух строках означают, что 1С не
    # отдаёт выгрузку, и остаток в приложении больше не сверяется со складом.
    shared_workers = ["dispatch", "ftp_send", "ftp_receive", "reconciliation",
                      "reconciliation_applied"]
    shared_heartbeats = [{"name": w, "hb": _heartbeat_for(db, w)} for w in shared_workers]

    report_findings = collect_findings(db)

    global_stats = {
        "conflicts": db.query(MappingConflict).count(),
        "anomalies_new": db.query(SyncAnomaly).filter(
            SyncAnomaly.status == AnomalyStatus.new, SyncAnomaly.is_test.is_(False),
        ).count(),
        "dispatch_errors_total": len(current_dispatch_errors(db)),
    }

    return templates.TemplateResponse(request, "diagnostics.html", {
        "request": request, "current_user": user, "active_page": "diagnostics",
        "account_rows": account_rows, "shared_heartbeats": shared_heartbeats,
        "global_stats": global_stats, "now": now_utc(),
        "stuck_tasks": _stuck_rows(db),
        "repost_enabled": repost_enabled(), "max_reposts": MAX_REPOSTS,
        "flash": pop_flash(request),
        # Сводка отчёта о расхождениях — здесь, а не сам отчёт: «Диагностика»
        # отвечает на вопрос «жива ли система», отчёт — «где она разошлась с
        # реальностью». Смешать их значит получить страницу, которую читают по
        # диагонали. Но оператор ходит СЮДА, поэтому одну строку со ссылкой
        # показываем: иначе отчёт есть, а узнать о нём неоткуда.
        "report_findings": report_findings,
        "report_critical": sum(1 for f in report_findings if f.level == REPORT_CRITICAL),
    })


def _stuck_rows(db: Session) -> list[dict]:
    """Зависшие задания 1С с тем, что нужно человеку для решения: какой товар,
    сколько штук, сколько висит и что ответила 1С."""
    now = now_utc()
    rows = []
    for t in tasks_needing_review(db):
        product = None
        if t.barcode:
            bc = db.query(Barcode).filter(Barcode.barcode == t.barcode).first()
            if bc is not None:
                product = db.query(Product).filter(Product.uid_1c == bc.uid_1c).first()
        started = t.sent_at or t.created_at
        # Направление ошибки у создания и у отмены ПРОТИВОПОЛОЖНОЕ, и карточка
        # раньше объясняла оба одним текстом — по созданию. Открытое
        # `CREATE_MOVEMENT` считается «в пути» со знаком плюс: остаток занижен,
        # наружу уходит меньше, чем есть. Открытое `CANCEL_MOVEMENT` — со знаком
        # минус: остаток ЗАВЫШЕН, наружу уходит больше, чем есть, то есть риск
        # оверселла. Соответственно и решение «документа нет» по отмене остаток
        # не поднимает, а опускает. Оператор, читающий предупреждение буквально,
        # отказывался нажимать — и оставлял систему ровно в опасном состоянии.
        cancel = t.command == "CANCEL_MOVEMENT"
        rows.append({
            "task": t,
            "product": product,
            "age_hours": round((now - started).total_seconds() / 3600, 1) if started else None,
            "account": t.account.name if t.account else str(t.account_id),
            "is_cancel": cancel,
            "effect": ("остаток завышен на {} — наружу уходит больше, чем есть "
                       "(риск оверселла)".format(t.quantity) if cancel else
                       "остаток занижен на {} — наружу уходит меньше, чем есть"
                       .format(t.quantity)),
            # Что произойдёт по кнопке «документа нет».
            "no_document_effect": ("остаток УМЕНЬШИТСЯ на {}: 1С товар не вернула, "
                                   "и возвращать его нам тоже не за чем"
                                   .format(t.quantity) if cancel else
                                   "остаток ВЫРАСТЕТ на {}: 1С единицу не списала"
                                   .format(t.quantity)),
        })
    return rows


@router.post("/diagnostics/accounts/{account_id}/test-connection")
def test_connection(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/diagnostics", status_code=303)

    try:
        client = build_client(db, account_id)
        ok, message = client.test_connection()
    except CredentialsMissing as e:
        ok, message = False, str(e)
    except Exception as e:
        ok, message = False, f"Неожиданная ошибка: {e}"

    account.last_connection_check_at = now_utc()
    account.last_connection_ok = ok
    account.last_connection_message = message
    log_action(db, user.username, "connection_test", f"{account.name}: {'OK' if ok else 'FAIL'} — {message}")
    db.commit()

    set_flash(request, f"«{account.name}»: {message}", "good" if ok else "warn")
    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/resend-all")
def resend_all(
    request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Поставить в очередь текущий остаток по ВСЕМ транслируемым товарам.

    Рассылка событийная: отправив число, она считает его доставленным и сама к
    нему не возвращается. Если на площадке наше число кто-то перетёр — а именно
    это делала вторая система во время перехода, — сдвинуть её картину нечем:
    событий по товару больше не будет, остаток-то не менялся. Эта команда и есть
    такое событие, поставленное руками.

    Ничего не обходит: каждая пара идёт через те же гейты, что и обычная
    доотправка. Товар без включённой трансляции и кабинет, которого не касался
    расчёт, в очередь не попадут — по ним ушёл бы ноль и обнулил живую карточку.

    Отправляет не сразу: кладёт в очередь, дальше её разбирает штатный цикл
    рассылки. Так команда не зависит от доступности площадки в момент нажатия, а
    сбой отправки повторяется обычным порядком.
    """
    stats = enqueue_resend_all(db)
    log_action(db, user.username, "resend_all",
               f"переотправка: товаров {stats['products']}, записей {stats['queued']}")
    db.commit()

    if not stats["queued"]:
        set_flash(request, "Переотправлять нечего: ни по одному транслируемому товару "
                           "нет кабинета, покрытого расчётом.", "warn")
    else:
        set_flash(request, f"В очередь поставлено {stats['queued']} записей "
                           f"по {stats['products']} товарам — уйдут ближайшими "
                           f"циклами рассылки.", "good")
    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/close-old-reconciliation")
def close_old_reconciliation(
    request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Закрыть старые расхождения сверки, которые ждут решения, которого нет.

    До сентябрьской правки крупная разница с 1С помечалась `needs_review` и
    ждала ручного решения. Теперь сверка применяет ЛЮБОЕ движение склада сама и
    ставит `resolved` тут же — то есть решать эти строки некому и незачем: по
    ним остаток давно переписан, а сами они остались висеть. 20.09 на бою таких
    было 909 штук от 14–16.09.

    Трогаем ТОЛЬКО журнальную пометку и ТОЛЬКО у старых записей. Остатки,
    очередь и площадки эта команда не касается вовсе: расхождения, по которым и
    правда стоит разобраться, отчёт показывает по свежим записям за сутки, и
    порог `RECONCILIATION_WINDOW` тут не при чём — свежие сюда не попадут.
    """
    cutoff = now_utc() - CLOSE_RECONCILIATION_OLDER_THAN
    # ПОРЦИЯМИ, а не одной транзакцией на всю выборку. Раньше кнопка поднимала в
    # память ORM-объекты по каждой подходящей строке и обновляла их одним
    # коммитом: замер на 250 тысячах строк — 3,8 с на загрузку, 10,9 с на
    # обновление, 857 МБ памяти, и всё это время писать в базу не может никто —
    # ни рассылка, ни приём заказов, ни приём ответов 1С. За `busy_timeout` в
    # тридцать секунд следует `database is locked`, нигде не перехваченный.
    # Атомарность тут не нужна: каждая строка независима, недоделанное доделает
    # следующее нажатие.
    closed = 0
    for _ in range(CLOSE_RECONCILIATION_MAX_CHUNKS):
        ids = [row[0] for row in db.query(ReconciliationLog.id).filter(
            ReconciliationLog.resolved.is_(False),
            ReconciliationLog.checked_at < cutoff,
        ).limit(CLOSE_RECONCILIATION_CHUNK).all()]
        if not ids:
            break
        db.query(ReconciliationLog).filter(ReconciliationLog.id.in_(ids)).update(
            {ReconciliationLog.resolved: True}, synchronize_session=False)
        db.commit()
        closed += len(ids)

    log_action(db, user.username, "reconciliation_closed_old",
               f"закрыто старых расхождений сверки: {closed}")
    db.commit()

    if not closed:
        set_flash(request, "Старых расхождений сверки нет — закрывать нечего.", "good")
    else:
        tail = ""
        if closed >= CLOSE_RECONCILIATION_CHUNK * CLOSE_RECONCILIATION_MAX_CHUNKS:
            tail = (" Это предел за одно нажатие — строки могли остаться, "
                    "нажмите ещё раз.")
        set_flash(request, f"Закрыто старых расхождений сверки: {closed}. "
                           f"Остатки не тронуты — это пометка в журнале.{tail}", "good")
    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/accounts/{account_id}/poll-now")
def poll_now(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Ручной запуск опроса заказов вне расписания — чтобы проверить
    поведение сразу после ввода ключей, не дожидаясь цикла планировщика."""
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/diagnostics", status_code=303)

    try:
        client = build_client(db, account_id)
        new_stats = poll_new_orders(db, client, account, PENDING_WAREHOUSE_NAME[account.platform])
        cancel_stats = poll_cancellations(db, client, account)
        message = f"Новые: {new_stats}. Отмены: {cancel_stats}."
        log_action(db, user.username, "manual_poll_orders", f"{account.name}: {message}")
        db.commit()
        set_flash(request, f"«{account.name}»: {message}", "good")
    except CredentialsMissing as e:
        set_flash(request, f"«{account.name}»: {e}", "warn")
    except Exception as e:
        set_flash(request, f"«{account.name}»: ошибка при опросе — {e}", "warn")

    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/accounts/{account_id}/catalog-now")
def catalog_now(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/diagnostics", status_code=303)

    try:
        client = build_client(db, account_id)
        stats = load_platform_catalog(db, client, account)
        # Картина кластеров на «Сопоставлении площадок» собрана по СТАРОМУ
        # каталогу — после загрузки она врёт. Минута ожидания тут была бы
        # особенно обидной: человек нажал «Загрузить каталог» ровно затем, чтобы
        # увидеть новое.
        clear_clusters_cache()
        log_action(db, user.username, "manual_catalog_sync", f"{account.name}: {stats}")
        db.commit()
        set_flash(request, f"«{account.name}»: загружено {stats['fetched']} карточек, новых конфликтов {stats['new_conflicts']}.", "good")
    except CredentialsMissing as e:
        set_flash(request, f"«{account.name}»: {e}", "warn")
    except Exception as e:
        set_flash(request, f"«{account.name}»: ошибка при загрузке каталога — {e}", "warn")

    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/accounts/{account_id}/reset-failures")
def reset_failures(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Сбрасывает счётчик автоотключения вручную — например, после того как
    оператор поправил ключи и хочет дать кабинету новый шанс немедленно,
    не дожидаясь первого успешного цикла."""
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/diagnostics", status_code=303)

    account.consecutive_failures = 0
    account.last_error = None
    log_action(db, user.username, "failures_reset_manually", account.name)
    db.commit()

    set_flash(request, f"Счётчик сбоев для «{account.name}» сброшен.", "good")
    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/stuck-tasks/{task_id}/resolve")
def resolve_stuck(
    request: Request, task_id: int, document_exists: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Разбор зависшего задания решением человека, посмотревшего в 1С.

    Два исхода различаются не формулировкой, а последствием для остатка, поэтому
    флаг обязателен и не имеет умолчания: «документ создан» просто закрывает
    задание, «документа нет» ВОЗВРАЩАЕТ единицу в наш остаток. Пустое или
    неизвестное значение — отказ, а не догадка.
    """
    task = db.query(FtpTask).filter(FtpTask.id == task_id).first()
    if task is None:
        set_flash(request, "Задание не найдено.", "warn")
        return RedirectResponse("/diagnostics", status_code=303)
    if task.status not in (FtpTaskStatus.timeout, FtpTaskStatus.failed):
        set_flash(request, "Задание уже закрыто — разбирать нечего.", "info")
        return RedirectResponse("/diagnostics", status_code=303)
    if document_exists not in ("yes", "no"):
        set_flash(request, "Не указано, есть ли документ в 1С.", "warn")
        return RedirectResponse("/diagnostics", status_code=303)

    exists = document_exists == "yes"
    resolve_stuck_task(db, task, exists, user.username)
    log_action(db, user.username, "stuck_task_resolved",
               f"#{task.id} {task.command} заказ {task.order_id}: "
               f"документ в 1С {'найден' if exists else 'НЕ найден'}")
    db.commit()

    if exists:
        set_flash(request, f"Задание #{task.id} закрыто как проведённое.", "good")
    else:
        set_flash(request, f"Задание #{task.id} закрыто: документа в 1С нет. "
                           f"{task.quantity or 0} шт. вернутся в остаток после ближайшей сверки.",
                  "warn")
    return RedirectResponse("/diagnostics", status_code=303)
