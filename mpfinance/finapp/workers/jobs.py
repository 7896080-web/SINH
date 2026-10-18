"""Фоновые задания программы. Зовёт их `background.py` (потоки в процессе
службы) и тесты напрямую. Задание само ловит свои ошибки и пишет отметку."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from finapp import backup, catalog, config, exchange, mail, onec, settings
from finapp.database import SessionLocal
from finapp.finance import fetch
from finapp.models import OnecTask, OnecTaskStatus
from finapp.timeutils import now_utc
from finapp.workers.heartbeat import beat

logger = logging.getLogger("mpfinance.jobs")

DAILY = timedelta(hours=24)
# Не подтверждённую обработку спрашиваем снова не чаще раза в шесть часов:
# обновляют её руками в Конфигураторе, и ответ появится, когда это сделают.
PING_RETRY = timedelta(hours=6)
BACKUP_RETRY = timedelta(minutes=30)
_backup_failed_at: datetime | None = None


def _older_than(raw: str, age: timedelta) -> bool:
    if not raw:
        return True
    try:
        return now_utc() - datetime.fromisoformat(raw) > age
    except ValueError:
        return True


def job_onec_exchange() -> None:
    db = SessionLocal()
    try:
        got = onec.exchange_once(db)
        if got.get("disabled"):
            beat(db, "onec_exchange", True, "обмен с 1С выключен")
            return
        notes = []
        if got.get("unmatched"):
            notes.append(f"ответов 1С без задания: {got['unmatched']}")
        if got.get("stuck"):
            notes.append(f"заданий без ответа дольше срока: {got['stuck']}")
        if got.get("failed_files"):
            beat(db, "onec_exchange", False,
                 "не разобраны файлы ответов 1С: " + "; ".join(got["failed_files"] + notes))
            return
        beat(db, "onec_exchange", True, "; ".join(notes))
    except Exception as e:                       # noqa: BLE001
        logger.exception("1С: обмен упал")
        beat(db, "onec_exchange", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def _unanswered_recently(db, command: str) -> bool:
    """1С не ответила на эту команду за последние сутки. Пока 1С стоит, новое
    задание каждый час легло бы ещё одним файлом в общую папку заданий — к её
    возвращению их были бы десятки тяжёлых выгрузок в одном цикле с
    перемещениями Sync Admin. Хватит одного раза в сутки."""
    last = db.query(OnecTask).filter(OnecTask.command == command).order_by(OnecTask.id.desc()).first()
    return (last is not None and last.status == OnecTaskStatus.timeout.value
            and now_utc() - last.created_at < DAILY)


def job_daily() -> None:
    """Раз в сутки: проверка версии обработки (пока не подтверждена), справочник
    баркодов и себестоимость из 1С, чистка архива обмена. Пока обмен с 1С
    выключен — справочник из базы sync_admin."""
    db = SessionLocal()
    try:
        if not onec.enabled(db):
            if _older_than(settings.get(db, settings.SYNC_CATALOG_AT), DAILY):
                try:
                    stats = catalog.load_from_sync_admin(db)
                    db.commit()
                    logger.info("справочник из Sync Admin: %s", stats)
                except catalog.CatalogError as e:
                    db.rollback()
                    logger.warning("справочник из Sync Admin не загружен: %s", e)
                    # Причина — в отметку, её показывает «Диагностика»: лог
                    # читают, когда уже что-то случилось.
                    beat(db, "daily", True, f"обмен с 1С выключен; справочник из Sync Admin не загружен: {e}")
                    return
            beat(db, "daily", True, "обмен с 1С выключен")
            return
        if not onec.epf_ready(db):
            last_ping = db.query(OnecTask).filter(OnecTask.command == onec.PROBE) \
                .order_by(OnecTask.id.desc()).first()
            if last_ping is None or now_utc() - last_ping.created_at > PING_RETRY:
                onec.enqueue(db, onec.PROBE)
                db.commit()
        else:
            for command, stamp in (("BARCODE_DICT", settings.CATALOG_LOADED_AT),
                                   ("EXPORT_COST_PRICES", settings.COSTS_LOADED_AT)):
                if _older_than(settings.get(db, stamp), DAILY) and not _unanswered_recently(db, command):
                    onec.enqueue(db, command)
            db.commit()
        exchange.prune_archive(config.ONEC_ARCHIVE_DIR)
        beat(db, "daily", True, "")
    except Exception as e:                       # noqa: BLE001
        db.rollback()
        logger.exception("суточное задание упало")
        beat(db, "daily", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def job_finance_fetch() -> None:
    db = SessionLocal()
    try:
        beat(db, "finance_fetch", True)
        planned = fetch.plan_regular(db)
        stats = fetch.run_pending(db, heartbeat=lambda: beat(db, "finance_fetch", True))
        rates = fetch.ensure_rates(db)
        if planned or stats["jobs"] or rates:
            logger.info("загрузка отчётов: заявок поставлено %s, разобрано %s, загружено %s, "
                        "с ошибкой %s, курсов ЦБ %s", planned, stats["jobs"], stats["imported"],
                        stats["failed"], rates)
        # Неудачная заявка — не сбой задания: это настройка кабинета (нет токена
        # «Финансы», нет доступа), причина видна в журнале загрузок на странице
        # «Отчёты площадок».
        beat(db, "finance_fetch", True)
    except Exception as e:                       # noqa: BLE001
        logger.exception("загрузка отчётов упала")
        beat(db, "finance_fetch", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def job_backup() -> None:
    global _backup_failed_at
    db = SessionLocal()
    try:
        last = backup.last_backup()
        if last is not None and now_utc() - last < backup.MIN_GAP:
            return   # отметку не трогаем: иначе каждый перезапуск сдвигал бы срок
        if _backup_failed_at is not None and now_utc() - _backup_failed_at < BACKUP_RETRY:
            return
        err = backup.make_backup()
        _backup_failed_at = now_utc() if err else None
        beat(db, "backup", not err, err)
    except Exception as e:                       # noqa: BLE001
        # Любой сбой (не только SQLite: переименование, которое держит
        # антивирус) — пауза BACKUP_RETRY. Без неё повтор шёл каждые 30 с и
        # оставлял по полноразмерной недописанной копии на общем диске.
        _backup_failed_at = now_utc()
        logger.exception("копия базы упала")
        beat(db, "backup", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def job_mail() -> None:
    """Письма с отчётами Lamoda — раз в 30 минут, если почта настроена."""
    db = SessionLocal()
    try:
        if not mail.load(db).configured:
            return
        stats = mail.fetch_new(db)
        if stats["messages"]:
            logger.info("почта: %s", stats)
        beat(db, "mail", not stats["failed"],
             f"писем с ошибкой: {stats['failed']}" if stats["failed"] else "")
    except Exception as e:                       # noqa: BLE001
        db.rollback()
        logger.warning("почта: %s", e)
        beat(db, "mail", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()
