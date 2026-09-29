"""Фоновый воркер программы (служба `marking_worker`).

Задания:
- `onec_exchange` каждые 30 с: положить ждущие задания в 1С, разобрать ответы,
  отметить зависшие;
- `nk_fetch` каждую минуту: карточки Нацкаталога в пределах лимита;
- `backup` раз в сутки; первый прогон — через 10 минут после старта, чтобы
  суточное задание на `interval` не откладывалось каждым перезапуском воркера
  (у sync_admin выгрузка каталога так не запускалась ни разу).
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from markapp import backup, nk, onec
from markapp.database import Base, SessionLocal, engine
from markapp.settings import ensure_defaults
from markapp.timeutils import now_utc
from markapp.workers.heartbeat import beat

logger = logging.getLogger("marking.worker")


def job_onec_exchange() -> None:
    db = SessionLocal()
    try:
        sent = onec.publish_pending(db)
        got = onec.collect_results(db)
        stuck = onec.mark_timeouts(db)
        notes = []
        if got["unmatched"]:
            # Ответ, не легший ни на одно задание, уже в архиве и не вернётся.
            notes.append(f"ответов 1С без задания: {got['unmatched']}")
        if stuck:
            notes.append(f"заданий без ответа дольше срока: {stuck}")
        beat(db, "onec_exchange", True, "; ".join(notes))
        if sent or got["files"]:
            logger.info("1С: отправлено строк %s, разобрано файлов %s (%s)", sent, got["files"], got)
    except Exception as e:
        logger.exception("1С: обмен упал")
        beat(db, "onec_exchange", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def job_nk_fetch() -> None:
    """Карточки Нацкаталога — в пределах лимита (10 запросов за 5 минут)."""
    db = SessionLocal()
    try:
        stats = nk.fetch_due(db)
        beat(db, "nk_fetch", True, stats.get("note", ""))
    except Exception as e:
        logger.exception("НК: запрос карточек упал")
        beat(db, "nk_fetch", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def job_backup() -> None:
    db = SessionLocal()
    try:
        last = backup.last_backup()
        if last is not None and now_utc() - last < backup.MIN_GAP:
            return   # heartbeat не трогаем: иначе каждый рестарт сдвигал бы срок
        res = backup.make_backup()
        if res.error:
            beat(db, "backup", False, res.error)
        else:
            beat(db, "backup", True, res.remote_error)
    except Exception as e:
        logger.exception("бэкап упал")
        beat(db, "backup", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_defaults(db)
    finally:
        db.close()
    sched = BlockingScheduler()
    common = dict(misfire_grace_time=300, coalesce=True, max_instances=1)
    sched.add_job(job_onec_exchange, "interval", seconds=30, id="onec_exchange",
                  next_run_time=datetime.now(), **common)
    sched.add_job(job_nk_fetch, "interval", seconds=60, id="nk_fetch",
                  next_run_time=datetime.now() + timedelta(seconds=20), **common)
    sched.add_job(job_backup, "interval", hours=24, id="backup",
                  next_run_time=datetime.now() + timedelta(minutes=10), **common)
    logger.info("воркер маркировки запущен")
    sched.start()


if __name__ == "__main__":
    main()
