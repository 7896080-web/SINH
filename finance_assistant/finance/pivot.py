"""Сводная таблица бизнес-расходов за любой период: статьи × недели/месяцы.

Источник — те же данные, что у /itog: записанные бизнес-расходы (со статьями)
и списания с бизнес-счёта по выпискам, которые ещё не разнесены по статьям
(«Без статьи»). Личные расходы здесь не показываются — они считаются только
помесячно по выпискам (/itog).
"""

import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .reconcile import NO_CATEGORY, summarize
from .report import month_name
from .storage import BUSINESS, EXPENSE, Storage

MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]


@dataclass
class Item:
    date: str
    amount: int
    category: str
    merchant: str
    card: str
    source: str          # «скриншот» / «выписка»


@dataclass
class Period:
    start: date
    end: date
    title: str

    @property
    def columns(self) -> str:
        """Разбивка колонок: неделя — по дням, до двух месяцев — по неделям, иначе по месяцам."""
        days = (self.end - self.start).days + 1
        if days <= 7:
            return "day"
        if days <= 62:
            return "week"
        return "month"


def _fmt(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year}"


def _month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def period_presets(today: date) -> dict[str, Period]:
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    prev_month_end = month - timedelta(days=1)
    return {
        "week": Period(week, week + timedelta(days=6), "эта неделя"),
        "prevweek": Period(week - timedelta(days=7), week - timedelta(days=1), "прошлая неделя"),
        "month": Period(month, _month_end(month), "этот месяц"),
        "prevmonth": Period(prev_month_end.replace(day=1), prev_month_end, "прошлый месяц"),
        "year": Period(today.replace(month=1, day=1), today.replace(month=12, day=31), "этот год"),
    }


def parse_period(arg: str, today: date) -> Period | None:
    """'01.09.2026-15.09.2026' | '2026-09' | '2026' | ключ пресета (week, month…)."""
    arg = arg.strip().lower().replace(" ", "")
    presets = period_presets(today)
    if arg in presets:
        return presets[arg]
    if arg.isdigit() and len(arg) == 4:
        y = int(arg)
        return Period(date(y, 1, 1), date(y, 12, 31), f"{y} год")
    try:
        m = datetime.strptime(arg, "%Y-%m").date()
        return Period(m, _month_end(m), month_name(f"{m:%Y-%m}"))
    except ValueError:
        pass
    for sep in ("..", "-", "—", "–"):
        if arg.count(sep) == 1 and "." in arg:
            a, b = arg.split(sep)
            try:
                start = datetime.strptime(a, "%d.%m.%Y").date()
                end = datetime.strptime(b, "%d.%m.%Y").date()
            except ValueError:
                return None
            if start > end:
                return None
            return Period(start, end, f"{_fmt(start)} — {_fmt(end)}")
    return None


def business_items(storage: Storage, period: Period) -> list[Item]:
    start, end = period.start.isoformat(), period.end.isoformat()
    items = [
        Item(e.op_date, e.amount, e.category or NO_CATEGORY, e.merchant, e.card,
             "скриншот" if e.receipt_path else "запись")
        for e in storage.expenses_between(start, end)
        if e.kind == EXPENSE and e.purpose == BUSINESS
    ]
    # Бизнес-счёт: списания по выписке, которые ещё не разнесены по статьям.
    month = period.start.replace(day=1)
    while month <= period.end:
        m = f"{month:%Y-%m}"
        for cs in summarize(storage, m).business_accounts:
            lines = [ln for ln in cs.unmatched_out if start <= ln.op_date <= end]
            items += [Item(ln.op_date, ln.amount, NO_CATEGORY, ln.description, cs.card.name,
                           "выписка") for ln in lines]
            # Выписка только с итогами: остаток без дат — относим на конец месяца.
            rest = cs.business - cs.business_recorded - sum(ln.amount for ln in cs.unmatched_out)
            month_end = _month_end(month).isoformat()
            if rest > 0 and start <= month_end <= end:
                items.append(Item(month_end, rest, NO_CATEGORY, "по итогам выписки",
                                  cs.card.name, "выписка"))
        month = _month_end(month) + timedelta(days=1)
    return sorted(items, key=lambda i: (i.date, -i.amount))


def buckets(period: Period) -> list[tuple[str, date, date]]:
    out = []
    if period.columns == "day":
        d = period.start
        while d <= period.end:
            out.append((f"{d.day:02d}.{d.month:02d}", d, d))
            d += timedelta(days=1)
    elif period.columns == "week":
        d = period.start
        while d <= period.end:
            end = min(d + timedelta(days=6 - d.weekday()), period.end)
            out.append((f"{d.day:02d}.{d.month:02d}–{end.day:02d}.{end.month:02d}", d, end))
            d = end + timedelta(days=1)
    else:
        d = period.start
        while d <= period.end:
            end = min(_month_end(d), period.end)
            out.append((f"{MONTHS_SHORT[d.month - 1]} {d.year % 100:02d}", d, end))
            d = end + timedelta(days=1)
    return out


def pivot(items: list[Item], cols: list[tuple[str, date, date]]) -> dict[str, list[int]]:
    table: dict[str, list[int]] = {}
    for it in items:
        d = date.fromisoformat(it.date)
        row = table.setdefault(it.category, [0] * len(cols))
        for i, (_, a, b) in enumerate(cols):
            if a <= d <= b:
                row[i] += it.amount
                break
    return dict(sorted(table.items(), key=lambda kv: -sum(kv[1])))


def by_merchant(items: list[Item]) -> dict[str, dict[str, int]]:
    """Раскладка: статья → получатель → сумма."""
    out: dict[str, dict[str, int]] = {}
    for it in items:
        m = out.setdefault(it.category, {})
        name = it.merchant or "без получателя"
        m[name] = m.get(name, 0) + it.amount
    ordered = sorted(out.items(), key=lambda kv: -sum(kv[1].values()))  # как в сводной
    return {cat: dict(sorted(v.items(), key=lambda kv: -kv[1])) for cat, v in ordered}


def svod_text(period: Period, items: list[Item], rub) -> str:
    total = sum(i.amount for i in items)
    out = [f"📊 Бизнес-расходы: {period.title}",
           f"{_fmt(period.start)} — {_fmt(period.end)}", ""]
    if not items:
        return "\n".join(out + ["Бизнес-расходов за этот период нет."])
    out.append(f"💼 Всего: {rub(total)} ({len(items)} операций)")
    layout = by_merchant(items)
    for cat, row in pivot(items, [("", period.start, period.end)]).items():
        cat_total = row[0]
        out.append("")
        out.append(f"▪️ {cat}: {rub(cat_total)} · {cat_total * 100 / total:.0f}%")
        merchants = list(layout[cat].items())
        for name, amount in merchants[:5]:
            out.append(f"    {name}: {rub(amount)}")
        if len(merchants) > 5:
            rest = sum(a for _, a in merchants[5:])
            out.append(f"    ещё {len(merchants) - 5}: {rub(rest)}")
    cols = buckets(period)
    if len(cols) > 1:
        out.append("")
        out.append("По " + {"day": "дням", "week": "неделям", "month": "месяцам"}[period.columns] + ":")
        for (label, a, b) in cols:
            s = sum(i.amount for i in items if a <= date.fromisoformat(i.date) <= b)
            if s:
                out.append(f"  {label}: {rub(s)}")
    out.append("")
    out.append("Подробно — в Excel: сводная, раскладка по получателям и лист-фильтр по датам.")
    return "\n".join(out)


def svod_xlsx(period: Period, items: list[Item]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    bold = Font(bold=True)
    money = "# ##0.00"
    wb = Workbook()

    # 1. Сводная: статьи × недели/месяцы
    ws = wb.active
    ws.title = "Сводная"
    ws.append([f"Бизнес-расходы: {_fmt(period.start)} — {_fmt(period.end)}"])
    ws["A1"].font = bold
    ws.append([])
    cols = buckets(period)
    ws.append(["Статья"] + [c[0] for c in cols] + ["Итого", "Доля"])
    for c in ws[3]:
        c.font = bold
    table = pivot(items, cols)
    total = sum(i.amount for i in items) or 1
    for cat, row in table.items():
        ws.append([cat] + [v / 100 for v in row] + [sum(row) / 100, sum(row) / total])
    totals = [sum(r[i] for r in table.values()) / 100 for i in range(len(cols))]
    ws.append(["Итого"] + totals + [sum(totals), 1 if items else 0])
    for c in ws[ws.max_row]:
        c.font = bold
    for row in ws.iter_rows(min_row=4, min_col=2):
        for c in row[:-1]:
            c.number_format = money
        row[-1].number_format = "0%"
    ws.column_dimensions["A"].width = 34
    for i in range(2, len(cols) + 4):
        ws.column_dimensions[get_column_letter(i)].width = 14
    ws.freeze_panes = "B4"

    # 2. Раскладка: статья → получатель
    lay = wb.create_sheet("Раскладка")
    lay.append(["Статья / получатель", "Сумма", "Операций"])
    for c in lay[1]:
        c.font = bold
    counts: dict[tuple[str, str], int] = {}
    for it in items:
        key = (it.category, it.merchant or "без получателя")
        counts[key] = counts.get(key, 0) + 1
    for cat, merchants in by_merchant(items).items():
        lay.append([cat, sum(merchants.values()) / 100,
                    sum(n for (c, _), n in counts.items() if c == cat)])
        for c in lay[lay.max_row]:
            c.font = bold
        for name, amount in merchants.items():
            lay.append([f"    {name}", amount / 100, counts[(cat, name)]])
    for c in lay["B"][1:]:
        c.number_format = money
    lay.column_dimensions["A"].width = 44
    lay.column_dimensions["B"].width = 16

    # 3. Данные: все операции таблицей Excel — с фильтрами в заголовках
    data = wb.create_sheet("Данные")
    data.append(["Дата", "Неделя", "Месяц", "Статья", "Получатель", "Карта", "Сумма", "Источник"])
    for it in items:
        d = date.fromisoformat(it.date)
        iso = d.isocalendar()
        data.append([d, f"{iso[0]}-W{iso[1]:02d}", f"{d:%Y-%m}", it.category, it.merchant,
                     it.card, it.amount / 100, it.source])
    for c in data["A"][1:]:
        c.number_format = "DD.MM.YYYY"
    for c in data["G"][1:]:
        c.number_format = money
    for col, width in zip("ABCDEFGH", (12, 10, 9, 30, 34, 16, 14, 11)):
        data.column_dimensions[col].width = width
    if items:
        ref = f"A1:H{len(items) + 1}"
        tbl = Table(displayName="Operations", ref=ref)  # имя таблицы — латиницей, так надёжнее для Excel
        tbl.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        data.add_table(tbl)
    data.freeze_panes = "A2"

    # 4. Фильтр: меняете даты — суммы по статьям пересчитываются формулами
    flt = wb.create_sheet("Фильтр по датам")
    flt.append(["С", period.start])
    flt.append(["По", period.end])
    flt.append(["Поменяйте даты в B1 и B2 — суммы пересчитаются (в пределах выгруженных данных)."])
    flt.append([])
    flt.append(["Статья", "Сумма"])
    for r in (1, 2, 5):
        flt.cell(r, 1).font = bold
    flt.cell(5, 2).font = bold
    for c in ("B1", "B2"):
        flt[c].number_format = "DD.MM.YYYY"
    last = len(items) + 1
    for cat in table:
        r = flt.max_row + 1
        flt.cell(r, 1, cat)
        flt.cell(r, 2, f'=SUMIFS(Данные!$G$2:$G${last},Данные!$D$2:$D${last},A{r},'
                       f'Данные!$A$2:$A${last},">="&$B$1,Данные!$A$2:$A${last},"<="&$B$2)')
        flt.cell(r, 2).number_format = money
    r = flt.max_row + 1
    flt.cell(r, 1, "Итого").font = bold
    flt.cell(r, 2, f"=SUM(B6:B{r - 1})" if r > 6 else 0).number_format = money
    flt.column_dimensions["A"].width = 34
    flt.column_dimensions["B"].width = 16

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
