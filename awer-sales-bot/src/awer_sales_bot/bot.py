"""Бот MAX: long polling + расписание в одном процессе.

Привязка продавца: админка/CLI выдаёт код (или ссылку max.ru/<бот>?start=<код>), продавец
отправляет код боту → его user_id записывается в sellers.max_user_id.
Команды продавца: /plan — статус на сейчас, /help. Владелец (max.owners): /svodka.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import jobs, messages
from .config import Settings
from .db import DB
from .max_api import MaxClient

log = logging.getLogger(__name__)
HELP = ("Я присылаю каждое утро ваш план, сколько осталось до базы и до следующей ступени, средний чек, "
        "вещей в чеке и советы.\n/plan — состояние на сейчас\n/help — помощь")


def _extract(u: dict) -> tuple[str, int | None, str]:
    """(тип, user_id, текст/payload) из апдейта MAX."""
    t = u.get("update_type", "")
    if t == "bot_started":
        user = u.get("user") or {}
        return t, user.get("user_id"), (u.get("payload") or "")
    if t == "message_created":
        msg = u.get("message") or {}
        sender = msg.get("sender") or {}
        return t, sender.get("user_id"), ((msg.get("body") or {}).get("text") or "").strip()
    return t, None, ""


def handle(db: DB, cfg: Settings, client: MaxClient, u: dict) -> None:
    t, uid, text = _extract(u)
    if not uid:
        return
    seller = db.seller_by_user(uid)
    is_owner = uid in cfg.max.owners
    reply = None
    if t == "bot_started" and text:
        s = db.link_by_code(text, uid)
        reply = f"Готово, {s['first_name']}! {HELP}" if s else "Код не найден. Возьмите новый у администратора."
    elif t == "bot_started":
        reply = HELP if seller or is_owner else "Здравствуйте! Отправьте код, который выдал администратор."
    elif text.lower() in ("/help", "помощь"):
        reply = HELP
    elif text.lower() in ("/svodka", "сводка") and is_owner:
        reply = messages.owner_summary(db, cfg, date.today() - timedelta(days=1))
    elif text.lower() in ("/plan", "план"):
        reply = messages.daily(db, cfg, seller["id"], date.today() - timedelta(days=1)) if seller else \
            "Сначала отправьте код от администратора."
    elif not seller and len(text) == 6:
        s = db.link_by_code(text, uid)
        reply = f"Готово, {s['first_name']}! {HELP}" if s else "Код не найден. Возьмите новый у администратора."
    else:
        reply = HELP
    if reply:
        client.send(reply, user_id=uid)


def poll_forever(db: DB, cfg: Settings, client: MaxClient, stop: threading.Event) -> None:
    marker = int(db.get_kv("max_marker") or 0) or None
    backoff = 5
    while not stop.is_set():
        try:
            res = client.updates(marker, timeout=cfg.max.poll_timeout)
            for u in res.get("updates", []):
                try:
                    handle(db, cfg, client, u)
                except Exception:  # noqa: BLE001
                    log.exception("update failed: %s", u)
            if res.get("marker") is not None:
                marker = res["marker"]
                db.set_kv("max_marker", str(marker))
            backoff = 5
        except Exception:  # noqa: BLE001
            log.exception("polling error, retry in %ss", backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)


def run(cfg: Settings, db: DB) -> None:
    client = MaxClient(cfg.max_token, cfg.max.base_url, cfg.max.auth)
    log.info("MAX bot: %s", client.me())
    sender = jobs.Sender(db, cfg, client)
    sched = BackgroundScheduler(timezone=cfg.timezone)
    hh, mm = map(int, cfg.schedule.daily_send_time.split(":"))

    def daily():
        # повторяем, пока день не закрыт (нет выгрузок по всем магазинам) или не наступил дедлайн
        if jobs.daily_run(db, cfg, sender) == "waiting":
            sched.add_job(daily, "date", run_date=datetime.now() + timedelta(minutes=cfg.schedule.retry_minutes))

    sched.add_job(daily, "cron", hour=hh, minute=mm, id="daily")
    sched.start()
    stop = threading.Event()
    try:
        poll_forever(db, cfg, client, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        sched.shutdown()
        time.sleep(0.1)
