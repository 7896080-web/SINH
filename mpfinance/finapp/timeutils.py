"""Время: отметки — UTC (наивные, как все DateTime-колонки), календарные дни — местные.

Боевой сервер стоит в Москве (UTC+3), машина разработки — в UTC. С полуночи до
трёх часов местное число уже на день больше UTC-шного, поэтому «сегодня» для
человека, для отчёта площадки и для 1С считается только `today_local()`, а не
`.date()` от UTC. Тесты гоняются в обоих поясах (`TZ=Europe/Moscow pytest`).
"""
from datetime import date, datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_local() -> date:
    return datetime.now().date()


def local_day_start_utc(day: date) -> datetime:
    """Начало МЕСТНЫХ суток `day` — момент в UTC (aware).

    `datetime(y, m, d, tzinfo=timezone.utc)` дал бы 03:00 по Москве: заказы
    первых трёх часов суток остались бы за границей запроса к площадке."""
    return datetime(day.year, day.month, day.day).astimezone(timezone.utc)


def local_date_of(moment: datetime) -> date:
    """Местное число момента. Наивный момент считается UTC-шным."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().date()
