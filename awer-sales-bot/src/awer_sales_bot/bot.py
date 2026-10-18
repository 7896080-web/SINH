"""Бот MAX: long polling + расписание в одном процессе.

Привязка продавца: админка/CLI выдаёт код (или ссылку max.ru/<бот>?start=<код>), продавец
отправляет код боту → его user_id записывается в sellers.max_user_id.
Команды продавца: /plan — статус на сейчас, /help. Владелец (max.owners): /svodka. Всем: /id — свой user_id.
Кнопки (message_callback): ответы на квиз после урока недели, «Похвалить» в сводке владельца — quiz.py.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import jobs, lessons, messages, quiz
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
    if t == "message_callback":  # нажатие кнопки: payload вместо текста
        cb = u.get("callback") or {}
        return t, (cb.get("user") or {}).get("user_id"), cb.get("payload") or ""
    return t, None, ""


def on_callback(db: DB, cfg: Settings, client: MaxClient, u: dict, uid: int, payload: str) -> None:
    """Кнопки квиза (продавец) и «Похвалить» (владелец)."""
    cb_id = (u.get("callback") or {}).get("callback_id")
    seller = db.seller_by_user(uid)
    if payload.startswith("quiz|") and seller:
        note, out = quiz.on_quiz(db, cfg, seller["id"], payload)
    elif payload.startswith("praise|") and uid in cfg.max.owners:
        got = quiz.on_praise(db, cfg, payload)
        out = []
        if not got:
            note = "Кнопка устарела"
        elif db.already_sent(payload, "praise", got[0]):
            note = "Уже отправлено"
        else:
            to = db.seller(got[0])["max_user_id"]
            if to:
                client.send(got[1], user_id=to)
                db.log_message(payload, "praise", got[0], to, "ok", got[1])
            note = "Отправлено 👏" if to else "Продавец не привязан к боту"
    else:
        note, out = "Кнопка устарела", []
    if cb_id:
        client.answer(cb_id, notification=note)
    for text, buttons in out:
        time.sleep(0.6)  # MAX: не больше 2 сообщений в секунду в один диалог
        client.send(text, user_id=uid, buttons=buttons)


def handle(db: DB, cfg: Settings, client: MaxClient, u: dict) -> None:
    t, uid, text = _extract(u)
    if not uid:
        return
    if t == "message_callback":
        on_callback(db, cfg, client, u, uid, text)
        return
    seller = db.seller_by_user(uid)
    is_owner = uid in cfg.max.owners
    reply = linked = None
    if t == "bot_started" and text:
        linked = db.link_by_code(text, uid)
        reply = f"Готово, {linked['first_name']}! {HELP}" if linked else "Код не найден. Возьмите новый у администратора."
    elif t == "bot_started":
        reply = HELP if seller or is_owner else "Здравствуйте! Отправьте код, который выдал администратор."
    elif text.lower() in ("/help", "помощь"):
        reply = HELP
    elif text.lower() == "/id":  # владельцу при настройке: user_id для max.owners
        reply = f"Ваш user_id в MAX: {uid}"
    elif text.lower() in ("/svodka", "сводка") and is_owner:
        reply = messages.owner_summary(db, cfg, date.today() - timedelta(days=1))
    elif text.lower() in ("/plan", "план"):
        reply = messages.daily(db, cfg, seller["id"], date.today() - timedelta(days=1)) if seller else \
            "Сначала отправьте код от администратора."
    elif not seller and len(text) == 6:
        linked = db.link_by_code(text, uid)
        reply = f"Готово, {linked['first_name']}! {HELP}" if linked else "Код не найден. Возьмите новый у администратора."
    else:
        reply = HELP
    if reply:
        client.send(reply, user_id=uid)
    if linked:
        welcome_newcomer(db, cfg, client, linked["id"], uid)


def welcome_newcomer(db: DB, cfg: Settings, client: MaxClient, sid: int, uid: int) -> None:
    """Сразу после привязки новичку — первый урок программы новичка (остальные — с утренней рассылкой)."""
    x = lessons.next_onboarding(db, cfg, sid)
    if not x:
        return
    key = f"onb-{x['shift']}"
    text = lessons.onboarding_text(x, len(lessons.load(cfg)["onboarding"]))
    time.sleep(0.6)  # MAX: не больше 2 сообщений в секунду в один диалог
    client.send(text, user_id=uid)
    lessons.mark_sent(db, sid, key)
    db.log_message(date.today().isoformat(), "onboarding", sid, uid, "ok", text)


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
