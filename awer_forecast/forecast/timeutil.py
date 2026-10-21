"""Время. Даты в БД — московские календарные дни (день продажи, день среза остатка),
моменты — naive UTC. Москва без перехода на летнее время с 2014 года, поэтому
фиксированное смещение: так не нужна база часовых поясов (на Windows её нет без tzdata)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3), "MSK")


def utcnow() -> datetime:
    """Текущий момент, naive UTC — в таком виде он хранится в TIMESTAMP-колонках."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def as_aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def msk_date(value: datetime) -> date:
    """Московский календарный день момента (naive считается UTC)."""
    return as_aware_utc(value).astimezone(MSK).date()


def today_msk() -> date:
    return datetime.now(MSK).date()
