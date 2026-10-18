"""Задания по расписанию: ежедневная рассылка, начало и закрытие месяца."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import messages
from .config import Settings
from .db import DB
from .importers.onec_sales import import_inbox
from .max_api import MaxClient
from .planning import service

log = logging.getLogger(__name__)


class Sender:
    """Отправка с журналом и защитой от повторов. dry_run — печать вместо отправки."""

    def __init__(self, db: DB, cfg: Settings, client: MaxClient | None, dry_run: bool = False):
        self.db, self.cfg, self.client, self.dry = db, cfg, client, dry_run

    def to_seller(self, day: str, kind: str, sid: int, text: str) -> None:
        if not text or self.db.already_sent(day, kind, sid):
            return
        uid = self.db.seller(sid)["max_user_id"]
        self._send(day, kind, sid, uid, text)

    def to_owners(self, day: str, kind: str, text: str) -> None:
        for uid in self.cfg.max.owners:
            self._send(day, kind, None, uid, text)

    def _send(self, day, kind, sid, uid, text):
        if self.dry or uid is None:
            print(f"--- {kind} {day} → {uid or 'не привязан'}\n{text}\n")
            self.db.log_message(day, kind, sid, uid, "dry" if self.dry else "no_user", text)
            return
        try:
            self.client.send(text, user_id=uid)
            self.db.log_message(day, kind, sid, uid, "ok", text)
        except Exception as e:  # noqa: BLE001
            log.exception("send failed")
            self.db.log_message(day, kind, sid, uid, "error", text, str(e))


def daily_run(db: DB, cfg: Settings, sender: Sender, today: date | None = None, force: bool = False) -> str:
    """Импорт выгрузок → проверка закрытия дня → (1-го числа) итоги/план месяца → рассылка за вчера.

    Возвращает 'sent' | 'waiting' (день не закрыт, повторить позже)."""
    today = today or date.today()
    day = today - timedelta(days=1)
    for rep in import_inbox(db, cfg):
        log.info("импорт %s: строк %s, дни %s", rep.file, rep.lines, rep.days)
        if rep.unknown_sellers:
            sender.to_owners(day.isoformat(), "unknown_sellers",
                             "Новые продавцы в выгрузке 1С: " + ", ".join(sorted(rep.unknown_sellers)) +
                             ". Проверьте роль и привязку в админке.")
    missing = db.day_closed(day, cfg.stores)
    deadline = datetime.combine(today, datetime.strptime(cfg.schedule.deadline_time, "%H:%M").time())
    if missing and not force and datetime.now() < deadline:
        return "waiting"
    if missing:
        sender.to_owners(day.isoformat(), "missing_day", f"Нет выгрузки 1С за {day:%d.%m} по: {', '.join(missing)}. "
                                                          "Рассылка по имеющимся данным.")
    if today.day == 1:
        month_turn(db, cfg, sender, today)
    for s in db.sellers():
        if s["max_user_id"] and (db.worked_shifts(s["id"], day.strftime("%Y-%m"), upto=day) or
                                 db.one("SELECT 1 FROM plans WHERE month=? AND seller_id=?", day.strftime("%Y-%m"), s["id"])):
            sender.to_seller(day.isoformat(), "daily", s["id"], messages.daily(db, cfg, s["id"], day))
    sender.to_owners(day.isoformat(), "owner_daily", messages.owner_summary(db, cfg, day))
    return "sent"


def month_turn(db: DB, cfg: Settings, sender: Sender, today: date) -> None:
    """1-го числа: итоги прошлого месяца, фиксация и рассылка нового плана."""
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    cur = today.strftime("%Y-%m")
    if db.one("SELECT 1 FROM plans WHERE month=?", prev):
        for row in service.close_month(db, cfg, prev):
            sender.to_seller(cur, "month_result", row["seller_id"], messages.month_result(row))
    if not db.one("SELECT 1 FROM plans WHERE month=?", cur):
        service.freeze(db, cfg, cur)
    for s in db.sellers():
        sender.to_seller(cur, "month_start", s["id"], messages.month_start(db, cfg, s["id"], cur))
