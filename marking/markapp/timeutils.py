"""Время: отметки — UTC, календарные дни — местные.

Боевой сервер стоит в Москве (UTC+3), машина разработки — в UTC. С полуночи до
трёх часов местное число уже на день больше UTC-шного, поэтому «сегодня» для
человека и для 1С считается только `today_local()`, а не `.date()` от UTC.
"""
from datetime import date, datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now() -> datetime:
    return datetime.now().astimezone()


def today_local() -> date:
    return local_now().date()


def local_date_of(moment_utc: datetime) -> date:
    return moment_utc.replace(tzinfo=timezone.utc).astimezone().date()


def local_day_start_utc(d: date) -> datetime:
    """Начало местных суток `d` как наивное UTC-время (так хранятся отметки)."""
    return datetime(d.year, d.month, d.day).astimezone().astimezone(timezone.utc).replace(tzinfo=None)


def ru(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def parse_ru(text: str) -> date:
    dd, mm, yy = text.strip().split(".")
    return date(int(yy), int(mm), int(dd))
