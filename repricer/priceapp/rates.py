"""Курс доллара к рублю — от него себестоимость 1С ($) становится рублями.

Источник по умолчанию — ЦБ РФ: официальный ежедневный XML
(`https://www.cbr.ru/scripts/XML_daily.asp`), без ключа и без лимитов. Курс ЦБ
устанавливается на дату и публикуется заранее; берём то, что ЦБ отдаёт
«на сегодня». Есть режим «вручную» — когда считать надо по своему курсу.

Правила:
- **Нет курса — нет цены.** Расчёт без курса не подставляет «вчерашний где-то
  лежащий» молча: берётся последний ЗАПИСАННЫЙ курс ЦБ, и страница показывает
  его дату. Курса нет вовсе — расчёт отказывает словами.
- **Курс у каждого предложения свой** (`PriceChange.usd_rub`): по нему видно,
  от какого курса посчитана цена, а при отправке пол перепроверяется по
  ТЕКУЩЕМУ курсу — доллар мог вырасти между расчётом и подтверждением.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import requests
from sqlalchemy.orm import Session

from priceapp import config, settings
from priceapp.models import ExchangeRate
from priceapp.timeutils import now_utc

USD_CHAR_CODE = "USD"


class RateError(RuntimeError):
    pass


@dataclass
class Rate:
    usd_rub: Decimal
    rate_date: date | None
    source: str               # cbr / manual
    label: str                # для экрана: «ЦБ на 02.10.2026» / «вручную»


def parse_cbr_xml(content: bytes) -> tuple[date, Decimal]:
    """`<ValCurs Date="02.10.2026"><Valute><CharCode>USD</CharCode><Nominal>1</Nominal>
    <Value>81,5432</Value>…` → (дата, курс за 1 доллар)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise RateError(f"ответ ЦБ не разобран: {e}") from e
    raw_date = root.attrib.get("Date", "")
    try:
        rate_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
    except ValueError as e:
        raise RateError(f"в ответе ЦБ нет даты: {raw_date!r}") from e
    for v in root.findall("Valute"):
        if (v.findtext("CharCode") or "").strip() != USD_CHAR_CODE:
            continue
        try:
            nominal = Decimal((v.findtext("Nominal") or "1").strip())
            value = Decimal((v.findtext("Value") or "").strip().replace(",", "."))
        except InvalidOperation as e:
            raise RateError("курс доллара в ответе ЦБ не число") from e
        if nominal <= 0 or value <= 0:
            raise RateError("курс доллара в ответе ЦБ не положителен")
        return rate_date, (value / nominal).quantize(Decimal("0.0001"))
    raise RateError("в ответе ЦБ нет доллара США")


def fetch_cbr(db: Session, session: requests.Session | None = None) -> ExchangeRate:
    """Запросить курс у ЦБ и записать. Повторный запрос на ту же дату обновляет запись."""
    http = session or requests
    try:
        r = http.get(config.CBR_URL, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        raise RateError(f"ЦБ недоступен: {e}") from e
    rate_date, value = parse_cbr_xml(r.content)
    row = (db.query(ExchangeRate)
           .filter(ExchangeRate.rate_date == rate_date, ExchangeRate.source == "cbr").first())
    if row is None:
        row = ExchangeRate(rate_date=rate_date, source="cbr", usd_rub=value)
        db.add(row)
    row.usd_rub = value
    row.fetched_at = now_utc()
    db.commit()
    return row


def latest_cbr(db: Session) -> ExchangeRate | None:
    return (db.query(ExchangeRate).filter(ExchangeRate.source == "cbr")
            .order_by(ExchangeRate.rate_date.desc(), ExchangeRate.id.desc()).first())


def parse_manual(text: str) -> Decimal:
    raw = (text or "").strip().replace(" ", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation as e:
        raise RateError(f"курс «{text}» — не число") from e
    if not value.is_finite() or value <= 0 or value > 100000:
        raise RateError(f"курс «{text}» вне разумных пределов")
    return value.quantize(Decimal("0.0001"))


def current(db: Session) -> Rate | None:
    """Курс, по которому считать прямо сейчас. None — считать не по чему."""
    if settings.get(db, settings.RATE_MODE) == "manual":
        raw = settings.get(db, settings.RATE_MANUAL)
        if not raw:
            return None
        try:
            value = parse_manual(raw)
        except RateError:
            return None
        return Rate(value, None, "manual", "вручную")
    row = latest_cbr(db)
    if row is None:
        return None
    return Rate(Decimal(str(row.usd_rub)), row.rate_date, "cbr",
                f"ЦБ на {row.rate_date:%d.%m.%Y}")
