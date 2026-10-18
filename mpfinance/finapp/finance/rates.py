"""Курс доллара ЦБ РФ — им себестоимость 1С ($) становится рублями.

Себестоимость в 1С («Себестоимость номенклатуры», команда EXPORT_COST_PRICES)
ведётся в долларах — так же её читает репрайсер (`repricer/priceapp/rates.py`
соседней программы). Юнит-экономика переводит её в рубли курсом ЦБ НА ДАТУ
ОПЕРАЦИИ: продажа сентября считается по сентябрьскому курсу, а не по
сегодняшнему, иначе прибыль прошлых месяцев менялась бы вместе с долларом.

Источник — официальный XML ЦБ без ключа и лимитов: `XML_dynamic.asp` отдаёт
курс за любой диапазон одним запросом. ЦБ публикует курс по рабочим дням; на
выходные действует курс последнего рабочего дня — так и считаем.

Правило: **нет курса — нет себестоимости в рублях.** Ни нулём, ни «каким-то
прошлым» курсом молча не подставляем: такая продажа попадает в «продано без
себестоимости», и страница это показывает. Ручной курс из настроек — явный
запасной вариант, который включает человек.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from bisect import bisect_right
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from sqlalchemy.orm import Session

from finapp.models import CurrencyRate
from finapp.timeutils import now_utc

CBR_DYNAMIC_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"
USD_CBR_ID = "R01235"
E4 = Decimal(10000)
# За сколько дней до первой нужной даты просим курс: продажа в понедельник
# считается по курсу пятницы, и его тоже нужно иметь.
LOOKBACK_DAYS = 10


class RateError(RuntimeError):
    pass


def parse_cbr_dynamic(content: bytes) -> dict[date, Decimal]:
    """`<ValCurs><Record Date="02.09.2026" Id="R01235"><Nominal>1</Nominal>
    <Value>81,5432</Value></Record>…` → {дата: курс за 1 доллар}."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise RateError(f"ответ ЦБ не разобран: {e}") from e
    result = {}
    for record in root.findall("Record"):
        try:
            day = datetime.strptime(record.attrib.get("Date", ""), "%d.%m.%Y").date()
            nominal = Decimal((record.findtext("Nominal") or "1").strip())
            value = Decimal((record.findtext("Value") or "").strip().replace(",", "."))
        except (ValueError, InvalidOperation) as e:
            raise RateError(f"строка курса ЦБ не разобрана: {e}") from e
        if nominal <= 0 or value <= 0:
            raise RateError("курс доллара в ответе ЦБ не положителен")
        result[day] = (value / nominal).quantize(Decimal("0.0001"))
    if root.tag != "ValCurs" or (len(root) and not result):
        # Записи есть, а «Record» среди них нет — сменилась разметка ответа.
        # Пустой результат выглядел бы как «за эти дни курса нет», и курс
        # устаревал бы молча.
        raise RateError(f"ответ ЦБ другой разметки (корень «{root.tag}», записей {len(root)}) — "
                        f"формат изменился?")
    return result


def fetch_cbr_range(db: Session, date_from: date, date_to: date,
                    session: requests.Session | None = None) -> int:
    """Запросить курсы ЦБ за диапазон и записать. Возвращает, сколько дат записано."""
    http = session or requests
    params = {"date_req1": f"{date_from:%d/%m/%Y}", "date_req2": f"{date_to:%d/%m/%Y}",
              "VAL_NM_RQ": USD_CBR_ID}
    try:
        response = http.get(CBR_DYNAMIC_URL, params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        raise RateError(f"ЦБ недоступен: {e}") from e
    rates = parse_cbr_dynamic(response.content)
    for day, value in rates.items():
        row = db.query(CurrencyRate).filter(CurrencyRate.rate_date == day).first()
        if row is None:
            row = CurrencyRate(rate_date=day, source="cbr", usd_rub_e4=0)
            db.add(row)
        row.usd_rub_e4 = int((value * E4).to_integral_value())
        row.fetched_at = now_utc()
    db.commit()
    return len(rates)


def missing_range(db: Session, need_from: date, need_to: date) -> tuple[date, date] | None:
    """Какой диапазон дозапросить у ЦБ, чтобы покрыть [need_from, need_to].
    None — всё уже есть (курс есть на конец диапазона и не позже начала)."""
    first = db.query(CurrencyRate.rate_date).order_by(CurrencyRate.rate_date.asc()).first()
    last = db.query(CurrencyRate.rate_date).order_by(CurrencyRate.rate_date.desc()).first()
    lookback = need_from - timedelta(days=LOOKBACK_DAYS)
    if first is None:
        return lookback, need_to
    start = lookback if first[0] > need_from else None
    end_needed = last[0] < need_to - timedelta(days=3)   # выходные: свежее пятницы курса нет
    if start is None and not end_needed:
        return None
    return (start or last[0]), need_to


MAX_RATE_AGE_DAYS = 14


class RateBook:
    """Курс на дату: последний опубликованный не позже этой даты."""

    def __init__(self, db: Session, manual: Decimal | None = None):
        rows = db.query(CurrencyRate.rate_date, CurrencyRate.usd_rub_e4) \
            .order_by(CurrencyRate.rate_date.asc()).all()
        self._dates = [d for d, _ in rows]
        self._values = [Decimal(v) / E4 for _, v in rows]
        self.manual = manual

    def rate_on(self, day: date) -> Decimal | None:
        idx = bisect_right(self._dates, day) - 1
        # Последний опубликованный курс годится, только если он свежий: ЦБ не
        # публикует курс в выходные и праздники (дольше всего — новогодние), но
        # курс двухнедельной давности значит, что курсы перестали приходить, и
        # себестоимость молча считалась бы по старому. Тогда — ручной курс, а
        # нет его — «без себестоимости», как и обещано.
        if idx >= 0 and (day - self._dates[idx]).days <= MAX_RATE_AGE_DAYS:
            return self._values[idx]
        return self.manual

    def latest(self) -> tuple[date, Decimal] | None:
        if not self._dates:
            return None
        return self._dates[-1], self._values[-1]


def to_kop(cost_minor: int, currency: str, day: date, rates: RateBook) -> int | None:
    """Себестоимость в копейках на дату операции. None — перевести не по чему."""
    if currency == "RUB":
        return cost_minor
    if currency == "USD":
        rate = rates.rate_on(day)
        if rate is None:
            return None
        # 1 цент × (₽ за доллар) = (₽ за доллар) копеек.
        return int((Decimal(cost_minor) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return None
