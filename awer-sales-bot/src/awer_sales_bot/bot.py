"""Боты MAX: «План» и «Обучение» — long polling + расписание в одном процессе.

Привязка продавца: админка/CLI выдаёт код (или ссылку max.ru/<бот>?start=<код>), продавец
отправляет код боту «План» → его user_id записывается в sellers.max_user_id. Если подключён второй бот
(MAX_TRAINING_BOT_TOKEN), «План» сразу присылает кнопку «Подключить обучение» со своим кодом → user_id в боте
«Обучение» — sellers.training_user_id. Без второго бота всё обучение идёт в «План».
Команды продавца: /plan — статус на сейчас, /help. Владелец (max.owners): /svodka. Всем: /id — свой user_id.
Кнопки (message_callback): ответы на квиз после урока недели, «Похвалить» в сводке владельца — quiz.py.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import jobs, kb, lessons, messages, quiz, tasks
from .config import Settings
from .db import DB
from .max_api import MaxClient

log = logging.getLogger(__name__)
HELP = ("Я присылаю каждое утро ваш план, сколько осталось до базы и до следующей ступени, средний чек, "
        "вещей в чеке и советы.\n/plan — состояние на сейчас\n/help — помощь")
TRAINING_HELP = ("Здесь уроки, тесты и база знаний: ткани, утеплители, уход за вещами. Раз в неделю — урок "
                 "по вашей цели недели.\n/baza — база знаний; или просто напишите слово, например «кашемир»\n"
                 "/help — помощь")


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


def on_callback(db: DB, cfg: Settings, client: MaxClient, u: dict, uid: int, payload: str, seller=None) -> None:
    """Кнопки квиза (продавец) и «Похвалить» (владелец). seller — уже найденный продавец этого бота."""
    cb_id = (u.get("callback") or {}).get("callback_id")
    seller = seller or db.seller_by_user(uid)
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
    seller = db.seller_by_user(uid)
    if not cfg.training_token and (task_event(db, cfg, client, u, t, uid, text, seller) or
                                   kb_event(db, cfg, client, u, t, uid, text, seller, search=False)):
        return  # второго бота нет — задания и база знаний (/baza, без поиска по любому слову) здесь
    if t == "message_callback":
        on_callback(db, cfg, client, u, uid, text)
        return
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
        if cfg.training_token:  # второй бот: кнопка подключения, первый урок новичку придёт уже туда
            offer_training(db, cfg, client, linked["id"], uid)
        else:
            welcome_newcomer(db, cfg, client, linked["id"], uid)


def kb_event(db: DB, cfg: Settings, client: MaxClient, u: dict, t: str, uid: int, text: str, seller,
             search: bool = True) -> bool:
    """База знаний: /baza, кнопки разделов и статей (меняют то же сообщение), поиск по словам. True — обработано."""
    if t == "message_callback" and text.startswith("k|"):
        msg, rows = kb.on_callback(db, cfg, text, seller["id"] if seller else None)
        cb_id = (u.get("callback") or {}).get("callback_id")
        if cb_id:
            client.answer(cb_id, message={"text": msg, "format": "markdown", "attachments": [
                {"type": "inline_keyboard", "payload": {"buttons": rows}}]})
        return True
    if t != "message_created" or not text:
        return False
    if text.lower() in ("/baza", "база", "база знаний"):
        msg, rows = kb.home(db, cfg)
    elif search and seller and not text.startswith("/") and len(text) >= 3:
        msg, rows = kb.answer_search(db, cfg, text)
    else:
        return False
    client.send(msg, user_id=uid, buttons=rows)
    return True


def task_event(db: DB, cfg: Settings, client: MaxClient, u: dict, t: str, uid: int, text: str, seller) -> bool:
    """Тест, опрос, чек-лист: кнопка или свой ответ текстом. True — событие обработано."""
    if not seller:
        return False
    if t == "message_callback" and text[:2] in ("t|", "s|", "c|"):
        note, out, replace = tasks.on_callback(db, cfg, seller["id"], uid, text)
        cb_id = (u.get("callback") or {}).get("callback_id")
        if cb_id:
            client.answer(cb_id, notification=note, message=replace)
    elif t == "message_created" and text and not text.startswith("/"):
        out = tasks.on_text(db, cfg, seller["id"], uid, text)  # свой ответ в опросе
        if out is None:
            return False
    else:
        return False
    for msg, buttons in out:
        time.sleep(0.6)  # MAX: не больше 2 сообщений в секунду в один диалог
        client.send(msg, user_id=uid, buttons=buttons)
    return True


def offer_training(db: DB, cfg: Settings, client: MaxClient, sid: int, uid: int) -> None:
    """Кнопка «Подключить обучение» — ссылка на второй бот с кодом этого продавца."""
    code = db.new_training_code(sid)
    time.sleep(0.6)  # MAX: не больше 2 сообщений в секунду в один диалог
    if cfg.max_training.bot_link:
        client.send("Уроки, тесты и база знаний — в отдельном боте «Обучение», чтобы здесь остались только план "
                    "и показатели. Нажмите кнопку и «Начать».",
                    user_id=uid, buttons=[[{"type": "link", "text": "Подключить обучение",
                                            "url": f"{cfg.max_training.bot_link}?start={code}"}]])
    else:
        client.send(f"Уроки и тесты — в боте «Обучение». Найдите его в MAX и отправьте код {code}.", user_id=uid)


def handle_training(db: DB, cfg: Settings, client: MaxClient, u: dict) -> None:
    """События бота «Обучение»: подключение по коду из бота «План», кнопки квиза, помощь."""
    t, uid, text = _extract(u)
    if not uid:
        return
    seller = db.seller_by_training_user(uid)
    if t == "message_callback":
        if not task_event(db, cfg, client, u, t, uid, text, seller) and \
                not kb_event(db, cfg, client, u, t, uid, text, seller):
            on_callback(db, cfg, client, u, uid, text, seller=seller)
        return
    if task_event(db, cfg, client, u, t, uid, text, seller):
        return
    if text.lower() not in ("/help", "помощь", "/id") and not (not seller and len(text) == 6) and \
            kb_event(db, cfg, client, u, t, uid, text, seller):
        return  # /baza или поиск по базе знаний
    linked = None
    if text and (t == "bot_started" or (not seller and len(text) == 6)):
        linked = db.link_training(text, uid)
        reply = f"Готово, {linked['first_name']}! {TRAINING_HELP}" if linked else \
            "Код не найден. Нажмите «Подключить обучение» в боте «План» ещё раз."
    elif t == "bot_started" or not seller:
        reply = TRAINING_HELP if seller else "Сначала подключитесь к боту «План» — он пришлёт кнопку для подключения сюда."
    elif text.lower() == "/id":
        reply = f"Ваш user_id в MAX: {uid}"
    else:
        reply = TRAINING_HELP
    client.send(reply, user_id=uid)
    if linked:
        welcome_newcomer(db, cfg, client, linked["id"], uid)


def welcome_newcomer(db: DB, cfg: Settings, client: MaxClient, sid: int, uid: int) -> None:
    """Сразу после привязки новичку — первый урок программы новичка (остальные — с утренней рассылкой)."""
    x = lessons.next_onboarding(db, cfg, sid)
    if not x:
        return
    key = f"onb-{x['shift']}"
    text = lessons.onboarding_text(x, len(lessons.load(cfg, db)["onboarding"]))
    time.sleep(0.6)  # MAX: не больше 2 сообщений в секунду в один диалог
    client.send(text, user_id=uid)
    lessons.mark_sent(db, sid, key)
    db.log_message(date.today().isoformat(), "onboarding", sid, uid, "ok", text)


def poll_forever(db: DB, cfg: Settings, client: MaxClient, stop: threading.Event, handler=handle,
                 marker_key: str = "max_marker") -> None:
    marker = int(db.get_kv(marker_key) or 0) or None
    backoff = 5
    while not stop.is_set():
        try:
            res = client.updates(marker, timeout=cfg.max.poll_timeout)
            for u in res.get("updates", []):
                try:
                    handler(db, cfg, client, u)
                except Exception:  # noqa: BLE001
                    log.exception("update failed: %s", u)
            if res.get("marker") is not None:
                marker = res["marker"]
                db.set_kv(marker_key, str(marker))
            backoff = 5
        except Exception:  # noqa: BLE001
            log.exception("polling error, retry in %ss", backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)


def run(cfg: Settings, db: DB) -> None:
    client = MaxClient(cfg.max_token, cfg.max.base_url, cfg.max.auth)
    log.info("MAX bot «План»: %s", client.me())
    training = None
    if cfg.training_token:
        training = MaxClient(cfg.training_token, cfg.max.base_url, cfg.max.auth)
        log.info("MAX bot «Обучение»: %s", training.me())
    sender = jobs.Sender(db, cfg, client, training_client=training)
    sched = BackgroundScheduler(timezone=cfg.timezone)
    hh, mm = map(int, cfg.schedule.daily_send_time.split(":"))

    def daily():
        # повторяем, пока день не закрыт (нет выгрузок по всем магазинам) или не наступил дедлайн
        if jobs.daily_run(db, cfg, sender) == "waiting":
            sched.add_job(daily, "date", run_date=datetime.now() + timedelta(minutes=cfg.schedule.retry_minutes))

    sched.add_job(daily, "cron", hour=hh, minute=mm, id="daily")
    # тесты, опросы, чек-листы — каждые 15 минут проверяем, у каких рассылок сейчас тестовые часы
    sched.add_job(lambda: tasks.run_due(db, cfg, sender), "cron", minute="*/15", id="tasks")
    sched.start()
    stop = threading.Event()
    if training:  # у второго бота свой опрос событий и своя отметка marker
        threading.Thread(target=poll_forever, args=(DB(cfg.db_path), cfg, training, stop, handle_training,
                                                     "max_marker_training"), daemon=True, name="max-training").start()
    try:
        poll_forever(db, cfg, client, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        sched.shutdown()
        time.sleep(0.1)
