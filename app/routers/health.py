from datetime import datetime
from app.timeutils import now_utc

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import WorkerHeartbeat

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
    "reconciliation": 3600 * 3,
    "reconcile_accounts": 300 * 3,
    "import_barcodes": 900 * 3,
    # Метка недельного ПОЛНОГО импорта справочника 1С (scheduler.job_import_barcodes):
    # пишется раз в 7 дней, без своей записи здесь протухала бы через 10 минут и
    # держала /health в 503 всю неделю.
    "import_barcodes_full": 7 * 86400 * 3,
}
# Per-account воркеры пишут heartbeat с ДИНАМИЧЕСКИМ именем
# (`poll_orders_account_<id>`, `catalog_poll_account_<id>`) — сопоставляем по
# префиксу, иначе интервал не подхватится и суточный catalog_poll вечно "протух".
PREFIX_INTERVAL_SECONDS = {
    "poll_orders_account_": 120 * 3,
    "catalog_poll_account_": 86400 * 3,
}
DEFAULT_EXPECTED_SECONDS = 600


def _expected_seconds(worker_name: str) -> int:
    for prefix, seconds in PREFIX_INTERVAL_SECONDS.items():
        if worker_name.startswith(prefix):
            return seconds
    return EXPECTED_INTERVAL_SECONDS.get(worker_name, DEFAULT_EXPECTED_SECONDS)


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """Без авторизации — намеренно: предназначен для внешних систем
    мониторинга (Zabbix, Uptime Kuma и т.п.), не отдаёт ничего чувствительного,
    только имена воркеров и время последнего запуска."""

    heartbeats = db.query(WorkerHeartbeat).all()
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

    workers = []
    overall_ok = True

    for hb in heartbeats:
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
            "last_error": hb.last_error if not hb.last_success else None,
        })

    status_code = 200 if overall_ok else 503
    return JSONResponse(
        {"ok": overall_ok, "checked_at": now.isoformat(), "workers": workers},
        status_code=status_code,
    )
