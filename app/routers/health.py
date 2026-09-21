from datetime import datetime
from app.timeutils import now_utc

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PlatformAccount, WorkerHeartbeat

router = APIRouter()

# Ожидаемая периодичность — используется, чтобы решить, "протух" ли воркер.
# Берём с запасом x3 от реального интервала планировщика (раздел scheduler.py),
# чтобы не поднимать ложную тревогу на случайной задержке одного цикла.
# Имена глобальных воркеров — точное совпадение.
EXPECTED_INTERVAL_SECONDS = {
    "dispatch": 45 * 3,
    "ftp_send": 60 * 3,
    "ftp_receive": 60 * 3,
    "ftp_send_export_request": 3600 * 3,
    "ftp_send_barcode_request": 86400 * 3,
    # Само задание ходит смотреть папку ответов раз в 5 минут (выгрузку у 1С при этом
    # просим раз в час — это разные вещи, см. build_scheduler).
    "reconciliation": 300 * 3,
    # Не задание, а метка ФАКТА: сверка применила выгрузку 1С (scheduler.job_reconciliation).
    # Задание `reconciliation` выше отчитывается об успехе и тогда, когда сверять было
    # нечем — свежей выгрузки нет, ошибки не произошло. Разошлись эти два смысла на
    # боевом 17.09.2026: воркер два часа подряд писал «нет свежего файла выгрузки
    # остатков — пропуск», heartbeat при этом оставался зелёным, и /health показывал
    # 200 при фактически остановленной сверке. Три пропущенных часовых цикла подряд —
    # тот же порог, что и у самого задания.
    "reconciliation_applied": 3600 * 3,
    "reconcile_accounts": 300 * 3,
    "import_barcodes": 900 * 3,
    "recalc": 20 * 6,
    # Метка недельного ПОЛНОГО импорта справочника 1С (scheduler.job_import_barcodes):
    # пишется раз в 7 дней, без своей записи здесь протухала бы через 10 минут и
    # держала /health в 503 всю неделю.
    "import_barcodes_full": 7 * 86400 * 3,
    # Отчёт о расхождениях ходит раз в час. Без своей записи он протухал бы по
    # умолчанию через 10 минут и держал /health красным всё остальное время.
    "discrepancy_report": 3600 * 3,
    # Сверка отправленного с площадкой — раз в полчаса.
    "verify_stock": 1800 * 3,
}
# Per-account воркеры пишут heartbeat с ДИНАМИЧЕСКИМ именем
# (`poll_orders_account_<id>`, `catalog_poll_account_<id>`) — сопоставляем по
# префиксу, иначе интервал не подхватится и суточный catalog_poll вечно "протух".
PREFIX_INTERVAL_SECONDS = {
    "poll_orders_account_": 120 * 3,
    "catalog_poll_account_": 86400 * 3,
}
DEFAULT_EXPECTED_SECONDS = 600

# Отметка старта планировщика (scheduler.build_scheduler). Не воркер: в списке
# воркеров не показывается и на «протухание» не проверяется — по ней считается
# время работы планировщика, чтобы не объявлять пропавшим воркер, который просто
# ещё не успел отработать первый раз после рестарта.
SCHEDULER_START_MARKER = "scheduler_start"

# Воркеры, которых ОБЯЗАНО быть видно. Раньше /health проверял только те
# heartbeat-ы, которые уже есть в базе: если задание не навесилось вовсе (ошибка
# в расписании, потерянный job), его строки просто не было — и мониторинг
# оставался зелёным, хотя, например, часовой запрос выгрузки не делался и сверка
# стояла. Значение — сколько ждём ПЕРВОГО прогона после старта планировщика;
# до этого срока отсутствие воркера не считается проблемой.
REQUIRED_WORKERS = {
    "dispatch": 300,
    "ftp_send": 300,
    "ftp_receive": 300,
    "ftp_send_export_request": 600,
    "ftp_send_barcode_request": 600,
    "reconciliation": 900,
    # Первый снимок ждём три ЧАСОВЫХ цикла — их задаёт запрос выгрузки, а не частота
    # самой сверки: сверка идёт через 5 минут после запроса
    # выгрузки, и если 1С в это окно не уложилась, следующая попытка будет через час.
    "reconciliation_applied": 3 * 3600,
    "reconcile_accounts": 900,
    "import_barcodes": 1800,
    "recalc": 300,
    # Отчёт о расхождениях ходит раз в час. Он сам по себе ничего не чинит, но
    # если он перестанет собираться, это заметно не будет никак — потому и
    # перечислен здесь наравне с рабочими заданиями.
    "discrepancy_report": 2 * 3600,
    # Сверка остатков с площадкой: молча переставшая ходить сверка выглядит
    # точно так же, как сверка, которой нечего сказать.
    "verify_stock": 2 * 1800,
    # Бэкап. Единственное задание, поломка которого ничего не ломает СЕГОДНЯ —
    # и ровно поэтому его отсутствие заметить некому. Ждём первого прогона
    # полчаса (само задание просит его через пять минут после старта).
    "backup": 1800,
    # Чистка истории. Её отсутствие не видно вообще ничем, кроме растущей базы.
    "retention": 1800,
}

ACCOUNT_WORKER_PREFIXES = ("poll_orders_account_", "catalog_poll_account_")

# Per-account задания тоже ОБЯЗАНЫ отработать — по одному на каждый активный
# кабинет. В REQUIRED_WORKERS их не перечислить: имена зависят от id кабинета.
# Без этой проверки задание, которое не отработало НИ РАЗУ, было мониторингу
# невидимо: строки heartbeat нет, а проверяются только существующие строки. Ровно
# так и вышло 19.09 — суточная выгрузка каталога не запускалась вовсе (первый
# запуск откладывался на сутки, а процесс столько не живёт), снимок каталога Kit
# лежал пятидневной давности, и /health всё это время был зелёный. Значение —
# сколько ждём ПЕРВОГО прогона после старта планировщика.
ACCOUNT_REQUIRED_GRACE = {
    "poll_orders_account_": 600,
    "catalog_poll_account_": 1800,
}

# Текст ошибки воркера содержит, например, адрес эндпоинта площадки, а /health
# открыт без авторизации. Наружу отдаём только факт ошибки; сам текст видно на
# странице «Диагностика» — она под логином.
ERROR_PLACEHOLDER = "есть ошибка — текст на странице «Диагностика»"


def _expected_seconds(worker_name: str) -> int:
    for prefix, seconds in PREFIX_INTERVAL_SECONDS.items():
        if worker_name.startswith(prefix):
            return seconds
    return EXPECTED_INTERVAL_SECONDS.get(worker_name, DEFAULT_EXPECTED_SECONDS)


def account_id_from_worker(worker_name: str) -> int | None:
    """id кабинета из имени per-account воркера, иначе None."""
    for prefix in ACCOUNT_WORKER_PREFIXES:
        if worker_name.startswith(prefix):
            tail = worker_name[len(prefix):]
            if tail.isdigit():
                return int(tail)
    return None


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """Без авторизации — намеренно: предназначен для внешних систем
    мониторинга (Zabbix, Uptime Kuma и т.п.). Отдаёт только имена воркеров,
    время последнего запуска и факт ошибки — без текста ошибки и без имён
    кабинетов."""

    # Порядок ЗАДАН явно. Без `order_by` SQLite отдаёт строки как ему удобно, и
    # список воркеров в ответе мог меняться от запроса к запросу: человек,
    # который смотрит /health два раза подряд, видел бы разный порядок и искал
    # бы нужную строку глазами каждый раз. Плюс на этом спотыкался тест,
    # бравший первую строку как «ту самую».
    heartbeats = db.query(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_name).all()
    now = now_utc()

    # Ни одного heartbeat — планировщик ни разу не отчитался: либо не запущен,
    # либо упал до первого цикла. Для системы про остатки это НЕ "здоров":
    # без воркеров остатки молча не синхронизируются, а мониторинг обязан это
    # увидеть. Поэтому пустой ответ — 503, а не 200.
    if not heartbeats:
        return JSONResponse(
            {
                "ok": False,
                "checked_at": now.isoformat(),
                "workers": [],
                "reason": "Ни один воркер не отчитался — планировщик не запущен или недоступен.",
            },
            status_code=503,
        )

    started_at = None
    for hb in heartbeats:
        if hb.worker_name == SCHEDULER_START_MARKER:
            started_at = hb.last_run_at

    # Кабинет отключён (руками или предохранителем) — его per-account задание
    # снято, и heartbeat больше не обновляется. Раньше такая строка навсегда
    # оставляла /health в 503: штатное срабатывание защиты красило мониторинг и
    # делало его бесполезным. Планировщик теперь удаляет строку вместе с
    # заданием, но старые и «осиротевшие» записи всё равно пропускаем здесь.
    live_account_ids = {a.id for a in db.query(PlatformAccount)
                        .filter(PlatformAccount.is_active.is_(True)).all()}
    disabled_accounts = db.query(PlatformAccount).filter(
        PlatformAccount.is_active.is_(False)).count()

    workers = []
    overall_ok = True
    ignored = 0
    present = set()

    for hb in heartbeats:
        if hb.worker_name == SCHEDULER_START_MARKER:
            continue
        account_id = account_id_from_worker(hb.worker_name)
        if account_id is not None and account_id not in live_account_ids:
            ignored += 1
            continue

        present.add(hb.worker_name)
        expected = _expected_seconds(hb.worker_name)
        age_seconds = (now - hb.last_run_at).total_seconds()
        is_stale = age_seconds > expected
        is_ok = hb.last_success and not is_stale

        if not is_ok:
            overall_ok = False

        workers.append({
            "worker": hb.worker_name,
            "last_run_at": hb.last_run_at.isoformat(),
            "age_seconds": int(age_seconds),
            "last_success": hb.last_success,
            "stale": is_stale,
            "ok": is_ok,
            "last_error": None if hb.last_success else ERROR_PLACEHOLDER,
        })

    # Пропавшие воркеры: проверяем, только если знаем время старта планировщика
    # (метка появляется при первом запуске после обновления) — иначе на старой
    # базе мы бы объявили пропавшим всё сразу после деплоя.
    missing = []
    if started_at is not None:
        uptime = (now - started_at).total_seconds()
        for name, grace in REQUIRED_WORKERS.items():
            if name not in present and uptime > grace:
                missing.append(name)
        for prefix, grace in ACCOUNT_REQUIRED_GRACE.items():
            if uptime <= grace:
                continue
            for account_id in live_account_ids:
                name = f"{prefix}{account_id}"
                if name not in present:
                    missing.append(name)
        if missing:
            overall_ok = False

    status_code = 200 if overall_ok else 503
    return JSONResponse(
        {
            "ok": overall_ok,
            "checked_at": now.isoformat(),
            "workers": workers,
            "missing_workers": sorted(missing),
            "ignored_workers": ignored,
            "disabled_accounts": disabled_accounts,
        },
        status_code=status_code,
    )
