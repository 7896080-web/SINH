"""Фоновые задания программы. Зовёт их `background.py` (поток в процессе
программы — служб на офисном компьютере нет) и тесты напрямую."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from priceapp import accounts, backup, dispatch, etalon, follow, guard, onec, platforms, rates, reference, settings
from priceapp.database import SessionLocal
from priceapp.models import Account, ApiCredential, OnecTask, OnecTaskStatus
from priceapp.timeutils import now_utc, today_local
from priceapp.workers.heartbeat import beat

logger = logging.getLogger("repricer.jobs")

REFRESH_EVERY = timedelta(hours=24)


def job_onec_exchange() -> None:
    db = SessionLocal()
    try:
        got = onec.exchange_once(db)
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
    except Exception as e:
        logger.exception("1С: обмен упал")
        beat(db, "onec_exchange", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def job_rate(session=None) -> None:
    """Курс ЦБ: запрашиваем, если сегодняшнего ещё нет."""
    db = SessionLocal()
    try:
        last = rates.latest_cbr(db)
        if last is None or last.rate_date < today_local() or \
                now_utc() - last.fetched_at > timedelta(hours=6):
            rates.fetch_cbr(db, session)
        # Курс ушёл от курса эталонных цен дальше порога — опросить WB сразу, не ждать 6 часов.
        try:
            if reference.enabled(db) and reference.rate_moved(db):
                settings.put(db, settings.REF_POLL_NOW, "1")
                db.commit()
        except Exception:
            db.rollback()
        beat(db, "rate", True, "")
    except Exception as e:
        logger.warning("курс ЦБ: %s", e)
        beat(db, "rate", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def job_price_dispatch(client_factory=None) -> None:
    db = SessionLocal()
    try:
        stats = dispatch.run(db, client_factory)
        errors = [f"{k}: {v['error']}" for k, v in stats.items() if "error" in v]
        if any(v.get("sent") for v in stats.values()):
            from priceapp import overview
            overview.mark_dirty(db)
        # Цены ушли на WB (автоматически или по ручному подтверждению) — Kit пересчитается после
        # того, как WB их примет: опрос через FOLLOW_DELAY, а не сразу (площадка применяет не мгновенно).
        wb_names = {a.name for a in db.query(Account).filter(Account.platform.in_(follow.SOURCE_PLATFORMS))}
        if follow.enabled(db) and any(v.get("sent") for k, v in stats.items() if k in wb_names):
            settings.put(db, settings.FOLLOW_DUE_AT, (now_utc() + FOLLOW_DELAY).isoformat())
            db.commit()
        beat(db, "price_dispatch", True, "; ".join(errors))
        if stats:
            logger.info("цены: %s", stats)
    except Exception as e:
        logger.exception("отправка цен упала")
        beat(db, "price_dispatch", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def _older_than(raw: str, age: timedelta) -> bool:
    if not raw:
        return True
    try:
        return now_utc() - datetime.fromisoformat(raw) > age
    except ValueError:
        return True


def job_daily_refresh(client_factory=None, force_prices: bool = False) -> None:
    """Раз в сутки: себестоимость из 1С, каталоги и текущие цены кабинетов с
    ключами, затем проверка диапазонов безопасности. `force_prices` — первый
    прогон после запуска программы: текущие цены запрашиваются ВСЕГДА, даже если
    загружались меньше суток назад, — площадка могла сменить цену или скидку, пока
    программа была выключена."""
    db = SessionLocal()
    notes = []
    prices_loaded = False
    try:
        if _older_than(settings.get(db, settings.COST_LOADED_AT), REFRESH_EVERY):
            onec.enqueue_cost(db)
            db.commit()
        # Справочник баркодов — тоже раз в сутки: перевешенный в 1С баркод иначе
        # считался бы от себестоимости прежнего товара, а новые — «нет в 1С».
        # Только после подтверждения mark-3: старая обработка положила бы ответ в
        # общую папку sync_admin.
        if onec.epf_ready(db) and _older_than(settings.get(db, settings.DICT_LOADED_AT), REFRESH_EVERY):
            onec.enqueue_dict(db)
            db.commit()
        for acc in db.query(Account).filter(Account.is_active.is_(True)):
            if db.query(ApiCredential.id).filter(ApiCredential.account_id == acc.id).first() is None:
                continue
            if not acc.catalog_loaded_at or now_utc() - acc.catalog_loaded_at >= REFRESH_EVERY:
                try:
                    st = accounts.load_catalog(db, acc, accounts.client_for(db, acc, client_factory))
                    if st["truncated"]:
                        notes.append(f"{acc.name}: каталог выгружен не полностью")
                except Exception as e:
                    db.rollback()
                    notes.append(f"{acc.name}: каталог не загружен — {e}"[:200])
            if acc.platform in platforms.READS_PRICES and (
                    force_prices or not acc.prices_loaded_at or now_utc() - acc.prices_loaded_at >= REFRESH_EVERY):
                try:
                    st = accounts.load_prices(db, acc, accounts.client_for(db, acc, client_factory))
                    prices_loaded = True
                    if st["truncated"]:
                        notes.append(f"{acc.name}: цены выгружены не полностью")
                except Exception as e:
                    db.rollback()
                    notes.append(f"{acc.name}: цены не загружены — {e}"[:200])
        if prices_loaded:
            try:
                st = guard.run(db)
                if st["accounts"]:
                    logger.info("%s", guard.summary(st))
                    if st["eaten"] or st["stuck"]:
                        notes.append(guard.summary(st))
            except Exception as e:
                db.rollback()
                notes.append(f"диапазоны безопасности не проверены — {e}"[:200])
        from priceapp import overview
        overview.mark_dirty(db)
        beat(db, "daily_refresh", True, "; ".join(notes))
    except Exception as e:
        logger.exception("суточное обновление упало")
        beat(db, "daily_refresh", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


WB_PRICES_MAX_AGE = timedelta(minutes=5)
FOLLOW_DELAY = timedelta(minutes=5)


def _older_than_ts(raw: str) -> bool:
    """Срок (ISO) ещё не наступил? Да — True (ждём)."""
    try:
        return datetime.fromisoformat(raw) > now_utc()
    except ValueError:
        return False


def wb_poll_requested() -> bool:
    """Просили опросить WB вне расписания (курс ушёл за порог эталонных цен; цены ушли на WB и пора
    пересчитать Kit). Флаги снимаются."""
    db = SessionLocal()
    try:
        due = settings.get(db, settings.FOLLOW_DUE_AT)
        due_now = bool(due) and not _older_than_ts(due)
        if settings.get(db, settings.REF_POLL_NOW) == "1" or due_now:
            settings.put(db, settings.REF_POLL_NOW, "0")
            if due_now:
                settings.put(db, settings.FOLLOW_DUE_AT, "")
            db.commit()
            return True
        return False
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()


def job_wb_poll(client_factory=None, max_age: timedelta = WB_PRICES_MAX_AGE) -> None:
    """Опрос WB: текущие цены кабинетов, затем правило эталонной цены (`reference.py`) —
    при запуске программы, каждые 6 часов и сразу, когда курс ушёл за порог. Цены,
    загруженные меньше `max_age` назад (суточное обновление после запуска), повторно не
    запрашиваются."""
    db = SessionLocal()
    notes = []
    try:
        poll_platforms = ["wb"] + (list(follow.TARGET_PLATFORMS) if follow.enabled(db) else [])
        if etalon.auto_enabled(db):
            # площадки идут за эталоном — их текущие цены нужны свежими, чтобы видеть, что разошлось
            poll_platforms = ["wb", *etalon.FOLLOWERS]
        for acc in db.query(Account).filter(Account.is_active.is_(True), Account.platform.in_(poll_platforms)):
            if db.query(ApiCredential.id).filter(ApiCredential.account_id == acc.id).first() is None:
                continue
            if acc.prices_loaded_at and now_utc() - acc.prices_loaded_at < max_age:
                continue
            try:
                st = accounts.load_prices(db, acc, accounts.client_for(db, acc, client_factory))
                if st["truncated"]:
                    notes.append(f"{acc.name}: цены выгружены не полностью")
            except Exception as e:
                db.rollback()
                notes.append(f"{acc.name}: цены не загружены — {e}"[:200])
        # Эталон — цена после скидки продавца; правило возвращает и цену до скидки, и
        # скидку (автоакцию). ДО правила «Kit следует за WB»: оно берёт цену WB после
        # скидки, и занижение площадкой иначе увело бы за собой Kit.
        st = reference.run(db, client_factory=client_factory)
        logger.info("%s", reference.summary(st))
        notes += st.get("errors", [])
        if etalon.auto_enabled(db):
            # Все площадки следуют за эталоном (страница «Эталонные цены»); прежнее правило
            # «Kit за ценой WB для покупателя» тогда не работает — две логики двигали бы одну цену.
            est = etalon.follow_all(db, client_factory)
            logger.info("%s", etalon.summary(est))
            fst = {"approved": sum(1 for _ in est.get("sent", {}))}
        else:
            fst = follow.run(db)
            logger.info("%s", follow.summary(fst))
        if st["restored"] or fst.get("approved"):
            from priceapp import overview
            overview.mark_dirty(db)
        beat(db, "wb_poll", True, "; ".join(notes))
    except Exception as e:
        db.rollback()
        logger.exception("опрос WB упал")
        beat(db, "wb_poll", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


_backup_failed_at = None
BACKUP_RETRY = timedelta(minutes=30)


def job_backup() -> None:
    global _backup_failed_at
    db = SessionLocal()
    try:
        last = backup.last_backup()
        if last is not None and now_utc() - last < backup.MIN_GAP:
            return   # heartbeat не трогаем: иначе каждый перезапуск сдвигал бы срок
        # Не снялась — следующая попытка через полчаса, а не на каждом такте в 30 с
        # (полный диск иначе грузили бы копированием непрерывно).
        if _backup_failed_at is not None and now_utc() - _backup_failed_at < BACKUP_RETRY:
            return
        err = backup.make_backup()
        _backup_failed_at = now_utc() if err else None
        beat(db, "backup", not err, err)
    except Exception as e:
        logger.exception("копия базы упала")
        beat(db, "backup", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()


def attention_dirty() -> bool:
    db = SessionLocal()
    try:
        from priceapp import overview
        return settings.get(db, overview.DIRTY_KEY) == "1"
    finally:
        db.close()


def job_attention() -> None:
    """Счётчики «Внимания» по товарам — в фоне: на боевом каталоге это десятки
    секунд, страница их только показывает."""
    db = SessionLocal()
    try:
        from priceapp import overview
        from priceapp.routers.prices import product_rows
        overview.refresh_heavy(db, product_rows)
        beat(db, "attention", True, "")
    except Exception as e:
        logger.exception("счётчики «Внимания» упали")
        beat(db, "attention", False, f"{type(e).__name__}: {e}")
    finally:
        db.close()
