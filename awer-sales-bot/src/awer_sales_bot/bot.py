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
import os
import threading
import time
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import botcfg, health, jobs, kb, lessons, messages, quiz, tasks
from .config import Settings
from .db import DB
from .max_api import MaxAuthError, MaxClient

log = logging.getLogger(__name__)
HELP = ("Я присылаю каждое утро ваш план, сколько осталось до базы и до следующей ступени, средний чек, "
        "вещей в чеке и советы.\n/plan — состояние на сейчас\n/help — помощь")
TRAINING_HELP = ("Здесь уроки, тесты и база знаний: ткани, утеплители, уход за вещами. Раз в неделю — урок "
                 "по вашей цели недели.\n/baza — база знаний; или просто напишите слово, например «кашемир»\n"
                 "/help — помощь")


CODE_TRIES = 5  # неверных кодов за час с одного аккаунта MAX, дальше пауза: код всего из 6 знаков (аудит Н2)
BLOCKED = "Слишком много неверных кодов. Попробуйте через час или возьмите новый код у администратора."


def _by_code(db: DB, uid: int, code: str, link) -> tuple[object | None, bool]:
    """(продавец или None, заблокирован ли) — привязка по коду с ограничением неверных попыток."""
    since = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    if db.one("SELECT COUNT(*) n FROM message_log WHERE kind='bad_code' AND max_user_id=? AND sent_at>=?",
              uid, since)["n"] >= CODE_TRIES:
        return None, True
    linked = link(code, uid)
    if not linked:
        db.log_message(date.today().isoformat(), "bad_code", None, uid, "error", code[:20])
    return linked, False


def _who(u: dict) -> str:
    """Имя из профиля MAX (bot_started.user, message_created.message.sender) — для сверки, тот ли человек."""
    p = u.get("user") or (u.get("message") or {}).get("sender") or (u.get("callback") or {}).get("user") or {}
    return " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or p.get("name") or ""


def _extract(u: dict) -> tuple[str, int | None, str]:
    """(тип, user_id, текст/payload) из апдейта MAX. Сообщения из групп и от ботов — без user_id (не отвечаем)."""
    t = u.get("update_type", "")
    if t in ("bot_started", "bot_stopped"):
        user = u.get("user") or {}
        return t, user.get("user_id"), (u.get("payload") or "")
    if t == "message_created":
        msg = u.get("message") or {}
        sender = msg.get("sender") or {}
        if sender.get("is_bot") or (msg.get("recipient") or {}).get("chat_type", "dialog") != "dialog":
            return t, None, ""
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
    if t == "bot_stopped":
        _stopped(db, seller, uid)
        return
    if text and t != "message_callback" and botcfg.link_owner(db, cfg, text, uid, _who(u)):
        client.send("Вы подключены как владелец: каждое утро — сводка по продавцам, «Кого похвалить» и "
                    "предупреждения. /svodka — сводка сейчас.", user_id=uid)
        return
    if not cfg.training_token and (task_event(db, cfg, client, u, t, uid, text, seller) or
                                   kb_event(db, cfg, client, u, t, uid, text, seller, search=False)):
        return  # второго бота нет — задания и база знаний (/baza, без поиска по любому слову) здесь
    if t == "message_callback":
        on_callback(db, cfg, client, u, uid, text)
        return
    is_owner = uid in cfg.max.owners
    reply = linked = None
    link = lambda code, user: db.link_by_code(code, user, _who(u))  # noqa: E731
    if t == "bot_started" and text:
        linked, blocked = _by_code(db, uid, text, link)
        reply = BLOCKED if blocked else f"Готово, {linked['first_name']}! {HELP}" if linked else \
            "Код не найден. Возьмите новый у администратора."
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
        linked, blocked = _by_code(db, uid, text, link)
        reply = BLOCKED if blocked else f"Готово, {linked['first_name']}! {HELP}" if linked else \
            "Код не найден. Возьмите новый у администратора."
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
    if t == "bot_stopped":
        _stopped(db, seller, uid)
        return
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
        linked, blocked = _by_code(db, uid, text, db.link_training)
        reply = BLOCKED if blocked else f"Готово, {linked['first_name']}! {TRAINING_HELP}" if linked else \
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


def _stopped(db: DB, seller, uid: int) -> None:
    """Продавец остановил бота (bot_stopped) — в журнал, чтобы владелец видел, почему сообщения не доходят."""
    db.log_message(date.today().isoformat(), "bot_stopped", seller["id"] if seller else None, uid, "stopped", "")


AUTH_TEXT = "MAX не принял токен — нужен новый токен в .env и перезапуск службы"
AUTH_WAIT = 600  # секунд между проверками при отозванном токене — чаще бессмысленно


def _failed(db: DB, client: MaxClient, u: dict, err: Exception) -> None:
    """Событие не обработано: в журнал (видно владельцу) и человеку — просьба повторить. Marker всё равно
    сдвигается — иначе одно «ядовитое» событие остановит бота навсегда."""
    t, uid, text = _extract(u)
    db.log_message(date.today().isoformat(), "incoming", None, uid, "error", f"{t}: {text}"[:500], repr(err)[:300])
    if uid and t in ("message_created", "bot_started"):
        try:
            client.send("Не получилось обработать сообщение. Попробуйте ещё раз через минуту.", user_id=uid)
        except Exception:  # noqa: BLE001
            log.exception("не удалось сообщить об ошибке")


def commands(cfg: Settings, bot: str) -> list[tuple[str, str]]:
    """Меню команд (кнопка «/» в MAX). Команды владельца (/svodka, /id) в меню не показываем — оно общее для всех."""
    if bot == "training":
        return [("baza", "База знаний: ткани, утеплители, уход"), ("help", "Что умеет бот")]
    out = [("plan", "Мой план и продажи на сейчас"), ("help", "Что умеет бот")]
    if not cfg.training_token:  # второго бота нет — база знаний здесь
        out.insert(1, ("baza", "База знаний: ткани, утеплители, уход"))
    return out


def connect(db: DB, client: MaxClient, bot: str, stop: threading.Event, cfg: Settings | None = None) -> dict | None:
    """GET /me с повторами: нет связи при старте службы — ждём, а не падаем (аудит В5). None — если остановили."""
    backoff = 5
    while not stop.is_set():
        try:
            me = client.me()
            health.ok(db, bot)
            log.info("MAX bot %s: %s", health.NAMES[bot], me)
            if cfg:
                try:
                    client.set_commands(commands(cfg, bot))
                except Exception:  # noqa: BLE001 — без меню бот работает, команды можно набрать вручную
                    log.warning("MAX bot %s: меню команд не задано", health.NAMES[bot], exc_info=True)
            return me
        except MaxAuthError as e:
            log.error("MAX bot %s: %s", health.NAMES[bot], e)
            health.error(db, bot, AUTH_TEXT)
            stop.wait(AUTH_WAIT)
        except Exception as e:  # noqa: BLE001
            log.warning("MAX bot %s: нет связи (%s), повтор через %s с", health.NAMES[bot], e, backoff)
            health.error(db, bot, f"нет связи с MAX ({type(e).__name__})")
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)
    return None


def poll_forever(db: DB, cfg: Settings, client: MaxClient, stop: threading.Event, handler=handle,
                 marker_key: str = "max_marker", bot: str = "plan") -> None:
    marker = int(db.get_kv(marker_key) or 0) or None
    backoff = 5
    while not stop.is_set():
        try:
            res = client.updates(marker, timeout=cfg.max.poll_timeout)
            health.ok(db, bot)
            for u in res.get("updates", []):
                try:
                    handler(db, cfg, client, u)
                except Exception as e:  # noqa: BLE001
                    log.exception("update failed: %s", u)
                    _failed(db, client, u, e)
            if res.get("marker") is not None:
                marker = res["marker"]
                db.set_kv(marker_key, str(marker))
            backoff = 5
        except MaxAuthError as e:
            log.error("MAX bot %s: %s", health.NAMES[bot], e)
            health.error(db, bot, AUTH_TEXT)
            stop.wait(AUTH_WAIT)
        except Exception as e:  # noqa: BLE001
            log.exception("polling error, retry in %ss", backoff)
            health.error(db, bot, f"нет связи с MAX ({type(e).__name__})")
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)


def run(cfg: Settings, db: DB, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    client = MaxClient(cfg.max_token, cfg.max.base_url, cfg.max.ca_file)
    if connect(db, client, "plan", stop, cfg) is None:
        return
    training = MaxClient(cfg.training_token, cfg.max.base_url, cfg.max.ca_file) if cfg.training_token else None
    jdb = DB(cfg.db_path)  # у заданий по расписанию своё соединение — не делят транзакцию с опросом (аудит В6)
    sender = jobs.Sender(jdb, cfg, client, training_client=training)
    # пропущенный на минуты запуск (сон, нагрузка) выполняется, а не теряется; несколько пропущенных — один раз
    sched = BackgroundScheduler(timezone=cfg.timezone, job_defaults={"misfire_grace_time": 3600, "coalesce": True})
    hh, mm = map(int, cfg.schedule.daily_send_time.split(":"))
    busy = threading.Lock()

    def daily():
        if not busy.acquire(blocking=False):
            return  # рассылка уже идёт (догон и расписание совпали)
        try:
            # повторяем, пока день не закрыт (нет выгрузок по всем магазинам) или не наступил дедлайн
            if jobs.daily_run(jdb, cfg, sender) == "waiting":
                sched.add_job(daily, "date", run_date=datetime.now() + timedelta(minutes=cfg.schedule.retry_minutes),
                              id="daily_retry", replace_existing=True)
        except Exception as e:  # noqa: BLE001
            log.exception("daily failed")
            sender.to_owners(date.today().isoformat(), "daily_failed",
                             f"Утренняя рассылка не прошла: {e}. Бот повторяет каждые 15 минут; "
                             "подробности — data/service.log.", once=True)
        finally:
            busy.release()

    def catch_up():
        """Аудит В2: служба не работала во время рассылки (перезагрузка, сбой) или рассылка упала — догоняем
        рассылку за вчера. Признак — kv daily_done, а не время запуска."""
        due = datetime.now().strftime("%H:%M") >= cfg.schedule.daily_send_time
        if due and jdb.get_kv("daily_done") != (date.today() - timedelta(days=1)).isoformat() \
                and not sched.get_job("daily_retry"):
            daily()

    sched.add_job(daily, "cron", hour=hh, minute=mm, id="daily")
    sched.add_job(catch_up, "interval", minutes=15, id="catch_up", next_run_time=datetime.now() + timedelta(seconds=30))
    # тесты, опросы, чек-листы — каждые 15 минут проверяем, у каких рассылок сейчас тестовые часы
    sched.add_job(lambda: tasks.run_due(jdb, cfg, sender), "cron", minute="*/15", id="tasks")
    sched.start()
    if training:  # у второго бота свой опрос событий и своя отметка marker
        threading.Thread(target=guarded, args=(_training_loop, cfg, training, stop), daemon=True,
                         name="max-training").start()
    try:
        poll_forever(db, cfg, client, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        sched.shutdown(wait=False)
        time.sleep(0.1)


def _training_loop(cfg: Settings, client: MaxClient, stop: threading.Event) -> None:
    tdb = DB(cfg.db_path)
    if connect(tdb, client, "training", stop, cfg) is not None:
        poll_forever(tdb, cfg, client, stop, handle_training, "max_marker_training", "training")


class Runner:
    """Поток бота в службе (awer-bot serve): запуск со страницы «Бот MAX», когда токен появился, и перезапуск
    службы, когда токен сменили (клиенты MAX создаются при старте потока)."""

    def __init__(self, cfg: Settings):
        self.cfg, self.running = cfg, False
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self.running or not os.environ.get("MAX_BOT_TOKEN"):
                return False
            self.running = True
        threading.Thread(target=guarded, args=(run, self.cfg, DB(self.cfg.db_path)), daemon=True,
                         name="max-bot").start()
        return True

    def restart(self, delay: float = 1.5) -> None:
        """Завершить процесс после ответа браузеру — служба (NSSM) поднимет его заново с новым токеном."""
        def stop():
            log.warning("перезапуск службы со страницы «Бот MAX»")
            logging.shutdown()
            os._exit(0)
        threading.Timer(delay, stop).start()


def guarded(target, *args) -> None:
    """Поток бота упал из-за ошибки в программе — завершаем процесс целиком, служба (NSSM) его перезапустит.
    Иначе админка продолжает работать, а рассылки нет, и никто этого не замечает (аудит В5)."""
    try:
        target(*args)
        log.critical("поток %s завершился — перезапуск службы", threading.current_thread().name)
    except BaseException:  # noqa: BLE001
        log.critical("поток %s упал — перезапуск службы", threading.current_thread().name, exc_info=True)
    logging.shutdown()
    os._exit(1)
