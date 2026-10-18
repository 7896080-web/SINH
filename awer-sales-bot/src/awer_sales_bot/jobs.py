"""Задания по расписанию: ежедневная рассылка, начало и закрытие месяца."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from . import achievements, coach, crews, goals, health, kb, lessons, messages, quiz
from . import metrics as M
from .config import Settings
from .db import DB
from .importers.onec_sales import import_inbox
from .max_api import MaxAuthError, MaxClient, MaxDenied
from .planning import service

log = logging.getLogger(__name__)
RETURNS_SHARE = 0.08  # доля возвратов, с которой урок недели — про примерку и размер (recommendations, правило 5)


class Sender:
    """Отправка с журналом и защитой от повторов. dry_run — печать вместо отправки.

    MAX принимает не больше 2 сообщений в секунду в один диалог — между сообщениями одному человеку пауза."""

    GAP = 0.6  # секунд между сообщениями одному получателю
    # что уходит в бот «Обучение» (если он подключён); остальное — в бот «План»
    TRAINING_KINDS = {"lesson", "quiz", "onboarding", "coach", "task", "arrivals"}

    def __init__(self, db: DB, cfg: Settings, client: MaxClient | None, dry_run: bool = False,
                 training_client: MaxClient | None = None):
        self.db, self.cfg, self.client, self.dry = db, cfg, client, dry_run
        self.training = training_client
        self._last: dict[int, float] = {}

    def to_seller(self, day: str, kind: str, sid: int, text: str, buttons: list[list[dict]] | None = None) -> str | None:
        """Статус отправки ('ok', 'dry', 'no_user', 'stopped' — продавец остановил бота, 'error')
        или None, если отправлять нечего / уже отправлено."""
        if not text or self.db.already_sent(day, kind, sid):
            return None
        s = self.db.seller(sid)
        if kind in self.TRAINING_KINDS and (self.training or (self.dry and self.cfg.training_token)):
            return self._send(day, kind, sid, s["training_user_id"], text, buttons, self.training)
        return self._send(day, kind, sid, s["max_user_id"], text, buttons)

    def to_owners(self, day: str, kind: str, text: str, buttons: list[list[dict]] | None = None,
                  once: bool = False) -> None:
        """once — не слать повторно тому, кому сообщение этого вида за этот день уже ушло."""
        for uid in self.cfg.max.owners:
            if once and self.db.already_sent_to(day, kind, uid):
                continue
            self._send(day, kind, None, uid, text, buttons)

    def _send(self, day, kind, sid, uid, text, buttons=None, client: MaxClient | None = None) -> str:
        if self.dry or uid is None:
            print(f"--- {kind} {day} → {uid or 'не привязан'}\n{text}\n")
            status = "dry" if self.dry else "no_user"
            self.db.log_message(day, kind, sid, uid, status, text)
            return status
        wait = self._last.get(uid, 0) + self.GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            (client or self.client).send(text, user_id=uid, buttons=buttons)
            self.db.log_message(day, kind, sid, uid, "ok", text)
            return "ok"
        except MaxDenied as e:  # продавец остановил или заблокировал бота — не сбой, видно в журнале
            self.db.log_message(day, kind, sid, uid, "stopped", text, str(e))
            return "stopped"
        except Exception as e:  # noqa: BLE001
            if isinstance(e, MaxAuthError):
                health.error(self.db, "training" if client is not None and client is self.training else "plan",
                             "MAX не принял токен — нужен новый токен в .env и перезапуск службы")
            log.exception("send failed")
            self.db.log_message(day, kind, sid, uid, "error", text, str(e))
            return "error"
        finally:
            self._last[uid] = time.monotonic()


def daily_run(db: DB, cfg: Settings, sender: Sender, today: date | None = None, force: bool = False) -> str:
    """Импорт выгрузок → проверка закрытия дня → (новый месяц) итоги/план месяца → рассылка за вчера.

    Возвращает 'sent' | 'waiting' (день не закрыт, повторить позже). Повторный вызов за тот же день безопасен:
    продавцам и владельцу ничего не уйдёт дважды (журнал message_log) — на этом держится догон (bot.catch_up)."""
    today = today or date.today()
    day = today - timedelta(days=1)
    for rep in import_inbox(db, cfg):
        log.info("импорт %s: строк %s, дни %s", rep.file, rep.lines, rep.days)
        if rep.error:  # аудит В1: битый файл не останавливает рассылку, но владелец о нём знает
            sender.to_owners(today.isoformat(), f"bad_file:{rep.file}",
                             f"Файл из 1С «{rep.file}» не прочитан: {rep.error}. Он перенесён в data/archive/bad, "
                             "остальные файлы загружены. Выгрузите этот день из 1С ещё раз.", once=True)
        if rep.unknown_sellers:
            sender.to_owners(day.isoformat(), "unknown_sellers",
                             "Новые продавцы в выгрузке 1С: " + ", ".join(sorted(rep.unknown_sellers)) +
                             ". Подключите их к боту: админка → «Продавцы» → «Выдать код».", once=True)
    gone = db.q("SELECT s.name, SUM(l.amount) a FROM sales_lines l JOIN sellers s ON s.id=l.seller_id "
                "WHERE l.day=? AND s.active=0 AND s.role<>'system' AND l.day>s.fired_at GROUP BY s.id", day.isoformat())
    if gone:  # уволенный продавец в чеках — скорее всего, в 1С выбран не тот продавец
        sender.to_owners(day.isoformat(), "fired_sales",
                         f"В выгрузке 1С за {day:%d.%m} продажи уволенных: " +
                         ", ".join(f"{r['name']} — {messages.rub(r['a'])}" for r in gone) +
                         ". Проверьте продавца в документах 1С, иначе эти продажи не попадут в план работающего.",
                         once=True)
    month = today.strftime("%Y-%m")
    probs = crews.problems(db, cfg, today, today + timedelta(days=cfg.staffing.lookahead_days - 1))
    if probs and db.one("SELECT 1 FROM schedule WHERE substr(day,1,7)=?", month):
        sender.to_owners(today.isoformat(), "staffing", "График работы на ближайшие дни:\n" + "\n".join(probs) +
                         "\nПоправьте: админка → «График работы».", once=True)
    if not db.one("SELECT 1 FROM schedule WHERE substr(day,1,7)=?", month):
        sender.to_owners(month, "no_schedule", f"График работы на {month} не заполнен — бот считает «до базы» по норме "
                                               f"{cfg.norm_shifts} рабочих дней. Заполните: админка → «График работы».",
                         once=True)
    missing = db.day_closed(day, cfg.stores)
    deadline = datetime.combine(today, datetime.strptime(cfg.schedule.deadline_time, "%H:%M").time())
    if missing and not force and datetime.now() < deadline:
        return "waiting"
    if missing:
        sender.to_owners(day.isoformat(), "missing_day", f"Нет выгрузки 1С за {day:%d.%m} по: {', '.join(missing)}. "
                                                          "Рассылка по имеющимся данным.", once=True)
    if month_due(db, today):  # не «сегодня 1-е»: служба могла не работать 1-го числа (аудит В2)
        month_turn(db, cfg, sender, today)
    for r in service.ensure_plans(db, cfg, day.strftime("%Y-%m")):
        sender.to_owners(day.isoformat(), f"new_plan_{r['seller_id']}",
                         f"Новый продавец {r['name']}: план {messages.rub(r['plan'])} на полный месяц "
                         f"({'новичок' if r['method'] == 'newcomer' else r['method']}). "
                         "Изменить — админка → «Настройки месяца» → «План вручную».")
    goals.ensure_week(db, cfg, today)
    for s in db.sellers():
        if s["max_user_id"] and (db.worked_shifts(s["id"], day.strftime("%Y-%m"), upto=day) or
                                 db.one("SELECT 1 FROM plans WHERE month=? AND seller_id=?", day.strftime("%Y-%m"), s["id"])):
            sender.to_seller(day.isoformat(), "daily", s["id"], messages.daily(db, cfg, s["id"], day))
    good = achievements.praise(db, cfg, day)
    sender.to_owners(day.isoformat(), "owner_daily", messages.owner_summary(db, cfg, day),
                     quiz.praise_buttons(good, day) or None, once=True)
    training_run(db, cfg, sender, today)
    arrivals_run(db, cfg, sender, today, day)
    coach.run(db, cfg, sender, today)
    if not sender.dry:
        db.set_kv("daily_done", day.isoformat())  # строка состояния в админке и догон (bot.catch_up)
    return "sent"


def arrivals_run(db: DB, cfg: Settings, sender: Sender, today: date, day: date) -> None:
    """Карточка новинок (модель, бренд, цвет, размеры + статьи) — в бот «Обучение» тем, у кого сегодня рабочий день
    (нет графика на месяц — всем)."""
    card = kb.arrivals_card(db, cfg, day)
    if not card:
        return
    month = today.strftime("%Y-%m")
    for s in db.sellers():
        if db.scheduled_after(s["id"], month, today - timedelta(days=1)) is not None and \
                not db.one("SELECT 1 FROM schedule WHERE seller_id=? AND day=?", s["id"], today.isoformat()):
            continue
        sender.to_seller(day.isoformat(), "arrivals", s["id"], card[0], card[1])


def training_run(db: DB, cfg: Settings, sender: Sender, today: date) -> None:
    """Новичку — урок программы к следующей смене; остальным — урок недели (раз в неделю, по теме цели недели),
    если работали последние 4 недели."""
    week = goals.monday(today)
    since = (today - timedelta(days=cfg.motivation.goal_lookback_days)).isoformat()
    total = len(lessons.load(cfg, db)["onboarding"])
    for s in db.sellers():
        sid = s["id"]
        if not s["max_user_id"]:
            continue
        if lessons.newcomer_shifts(db, cfg, sid) is not None:
            x = lessons.next_onboarding(db, cfg, sid)  # не больше одного урока программы в день
            if x and sender.to_seller(today.isoformat(), "onboarding", sid, lessons.onboarding_text(x, total)) == "ok":
                lessons.mark_sent(db, sid, f"onb-{x['shift']}")
            continue
        if not cfg.training.weekly_lesson:
            continue
        if not db.one("SELECT 1 FROM sales_lines WHERE seller_id=? AND day>=?", sid, since):
            continue
        if db.already_sent(week.isoformat(), "lesson", sid):
            continue
        g = goals.get(db, sid, week)
        topic = g.topic if g else "general"
        me = M.seller(db, sid, date.fromisoformat(since), today - timedelta(days=1))
        if (me.returns_share or 0) > RETURNS_SHARE:  # тот же порог, что у совета про возвраты
            topic = "returns"
        lesson = lessons.pick(db, cfg, sid, topic)
        if lesson and sender.to_seller(week.isoformat(), "lesson", sid, lessons.text(lesson)) == "ok":
            lessons.mark_sent(db, sid, lesson["id"])
            if cfg.training.quizzes and lesson.get("quiz"):
                sender.to_seller(week.isoformat(), "quiz", sid, *quiz.question(lesson, 0))


def month_due(db: DB, today: date) -> bool:
    """Пора ли начинать месяц: план текущего месяца не зафиксирован или прошлый месяц с планом не закрыт."""
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return not db.one("SELECT 1 FROM plans WHERE month=?", today.strftime("%Y-%m")) or bool(
        db.one("SELECT 1 FROM plans WHERE month=?", prev) and not db.one("SELECT 1 FROM results WHERE month=?", prev))


def month_turn(db: DB, cfg: Settings, sender: Sender, today: date) -> None:
    """Начало месяца (1-го числа или первый запуск после него): итоги прошлого месяца, фиксация и рассылка
    нового плана. Сообщения за месяц уходят один раз — журнал по ключу месяца."""
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    cur = today.strftime("%Y-%m")
    if db.one("SELECT 1 FROM plans WHERE month=?", prev):
        for row in service.close_month(db, cfg, prev):
            sender.to_seller(cur, "month_result", row["seller_id"], messages.month_result(row))
    if not db.one("SELECT 1 FROM plans WHERE month=?", cur):
        service.freeze(db, cfg, cur)
    for s in db.sellers():
        sender.to_seller(cur, "month_start", s["id"], messages.month_start(db, cfg, s["id"], cur))
