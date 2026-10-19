"""Состояние ботов для строки вверху админки (аудит В5): связь с MAX, последняя ошибка, утренняя рассылка.

Бот пишет в kv: `bot_ok:<бот>` — время последнего успешного обращения к MAX, `bot_err:<бот>` — «время|текст»
последней ошибки (очищается при успехе); jobs.daily_run — `daily_done` (день, за который ушла рассылка).
Боты: plan — «План», training — «Обучение».
"""
from __future__ import annotations

import html
import os
from datetime import date, datetime, timedelta

from .config import Settings
from .db import DB

NAMES = {"plan": "«План»", "training": "«Обучение»"}
STALE = timedelta(minutes=10)  # опрос событий идёт каждые ~30 секунд: дольше 10 минут тишины — бот не работает


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ok(db: DB, bot: str) -> None:
    db.set_kv(f"bot_ok:{bot}", _now())
    if db.get_kv(f"bot_err:{bot}"):
        db.set_kv(f"bot_err:{bot}", "")


def error(db: DB, bot: str, text: str) -> None:
    db.set_kv(f"bot_err:{bot}", f"{_now()}|{text[:300]}")


def _t(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d:%H:%M}" if d.date() == date.today() else f"{d:%d.%m %H:%M}"


def bot_state(db: DB, bot: str, now: datetime | None = None) -> tuple[bool, str]:
    """(всё ли в порядке, текст) для одного бота."""
    now = now or datetime.now()
    last, err = db.get_kv(f"bot_ok:{bot}"), db.get_kv(f"bot_err:{bot}")
    if err:
        at, _, text = err.partition("|")
        return False, f"бот {NAMES[bot]}: {text} (с {_t(at)})"
    if not last:
        return False, f"бот {NAMES[bot]} ещё ни разу не выходил на связь с MAX"
    if now - datetime.fromisoformat(last) > STALE:
        return False, f"бот {NAMES[bot]} не выходил на связь с MAX с {_t(last)} — служба запущена?"
    return True, f"бот {NAMES[bot]} на связи ({_t(last)})"


def daily_state(db: DB, cfg: Settings, now: datetime | None = None) -> tuple[bool, str]:
    now = now or cfg.now()
    yesterday, hm = now.date() - timedelta(days=1), now.strftime("%H:%M")
    done = db.get_kv("daily_done")
    last = f" (последняя — за {date.fromisoformat(done):%d.%m})" if done else ""
    d, _, nums = (db.get_kv("daily_sent") or "").partition("|")
    sent, _, total = nums.partition("|")
    count = f": ушло {sent} из {total}" if d == yesterday.isoformat() and total else ""
    if done == yesterday.isoformat():
        at = db.one("SELECT MAX(sent_at) t FROM message_log WHERE kind='daily' AND status='ok'")
        return True, f"утренняя рассылка за {yesterday:%d.%m} прошла" + (f" в {_t(at['t'])}" if at["t"] else "") + count
    if count:  # аудит Р2: часть не ушла (сбой связи) — догон дошлёт, но владелец видит, что не всё
        return False, f"утренняя рассылка за {yesterday:%d.%m}{count}, бот досылает остальным"
    if hm < cfg.schedule.daily_send_time:
        return True, f"утренняя рассылка — в {cfg.schedule.daily_send_time}{last}"
    if hm < cfg.schedule.deadline_time:
        return True, f"рассылка за {yesterday:%d.%m} ждёт выгрузку 1С (до {cfg.schedule.deadline_time}){last}"
    return False, f"рассылки за {yesterday:%d.%m} нет, хотя пора{last}"


def line(db: DB, cfg: Settings) -> str:
    """HTML-строка состояния для каждой страницы админки."""
    parts: list[tuple[bool, str]] = []
    if not os.environ.get("MAX_BOT_TOKEN"):
        parts.append((False, "бот не подключён: нет токена"))
    else:
        parts.append(bot_state(db, "plan"))
        if cfg.training_token:
            parts.append(bot_state(db, "training"))
        parts.append(daily_state(db, cfg))
    bad = not all(good for good, _ in parts)
    text = " · ".join(html.escape(t) for _, t in parts)
    more = ' <a href="/bot">Бот MAX →</a>' if bad else ""
    return f'<p class="health{" bad" if bad else ""}">{"⚠ " if bad else ""}{text}{more}</p>'
