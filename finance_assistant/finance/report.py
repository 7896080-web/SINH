"""Тексты для чата и Excel-файл месячного свода."""

import io
from datetime import date

from .money import format_amount
from .reconcile import CardSummary, MonthSummary
from .storage import BUSINESS, REIMBURSEMENT, Expense, StatementLine

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь"]


def rub(kopecks: int | None) -> str:
    return "—" if kopecks is None else f"{format_amount(kopecks)} ₽"


def month_name(month: str) -> str:
    year, mon = month.split("-")
    return f"{MONTHS[int(mon) - 1]} {year}"


def short_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day:02d}.{d.month:02d}"


def expense_line(e: Expense) -> str:
    if e.kind == REIMBURSEMENT:
        what = "возмещение от бизнеса"
    elif e.purpose == BUSINESS:
        what = f"бизнес · {e.category or 'без статьи'}"
    else:
        what = "личное"
    who = f" · {e.merchant}" if e.merchant else ""
    return f"{short_date(e.op_date)}  {rub(e.amount)}  {e.card}  {what}{who}"


def expense_card(e: Expense) -> str:
    lines = [f"✅ Записано №{e.id}", f"{rub(e.amount)} · {short_date(e.op_date)} · {e.card}"]
    if e.kind == REIMBURSEMENT:
        lines.append("Возмещение от бизнеса")
    elif e.purpose == BUSINESS:
        lines.append(f"Бизнес · {e.category or 'без статьи'}")
    else:
        lines.append("Личный расход")
    if e.merchant or e.description:
        lines.append(" — ".join(x for x in (e.merchant, e.description) if x))
    return "\n".join(lines)


def card_text(cs: CardSummary, month: str) -> str:
    out = [f"💳 {cs.card.label} — {month_name(month)}"]
    if cs.has_statement:
        out.append(f"Пришло: {rub(cs.total_in)}")
        out.append(f"Ушло:   {rub(cs.total_out)}")
        if cs.own_in or cs.own_out:
            out.append(f"  переводы между своими картами: +{rub(cs.own_in)} / −{rub(cs.own_out)}")
        out.append(f"  на бизнес: {rub(cs.business)}")
        out.append(f"  на личное: {rub(cs.personal)}")
    else:
        out.append("Выписка не загружена — есть только записанное вами:")
        out.append(f"  на бизнес: {rub(cs.business)}")
        if cs.personal_marked:
            out.append(f"  помечено как личное: {rub(cs.personal_marked)}")
    if cs.reimbursed:
        out.append(f"Возмещено бизнесом на эту карту: {rub(cs.reimbursed)}")
    if cs.has_statement and not cs.lines_checked:
        out.append("(по строкам не сверялось — в выписке были только итоги)")
    if cs.missing:
        out.append("")
        out.append("⚠️ Записано, но не найдено в выписке (проверьте карту или дату):")
        out += [f"  №{e.id} {expense_line(e)}" for e in cs.missing]
    return "\n".join(out)


def _line(ln: StatementLine) -> str:
    # Время важно: в выписке банка «Россия» у всех оплат одно описание
    # («Оплата по QR-коду через СБП»), различить их можно только так.
    when = short_date(ln.op_date) + (f" {ln.op_time}" if ln.op_time else "")
    return f"#{ln.id}  {when}  {rub(ln.amount)}  {ln.description}"


def unmatched_text(lines: list[StatementLine]) -> str:
    out = ["Списания из выписки, которые сейчас считаются личными.",
           "Если среди них есть бизнес — отправьте /biz и номера, например: /biz 12 15", ""]
    return "\n".join(out + [_line(ln) for ln in lines])


def suggestions_text(lines: list[StatementLine]) -> str:
    out = [f"🔎 Похоже на бизнес-расходы: {len(lines)} на {rub(sum(ln.amount for ln in lines))}",
           "Проверьте список. Лишние уберите: /notbiz 12 15, недостающие добавьте: /biz 7",
           ""]
    out += [f"{_line(ln)} → {ln.suggested_category}" for ln in lines]
    return "\n".join(out)


def month_text(s: MonthSummary, owed_total: int | None = None) -> str:
    out = [f"📊 Свод за {month_name(s.month)}", ""]
    for cs in s.cards:
        state = "" if cs.has_statement else "  (нет выписки)"
        out.append(f"💳 {cs.card.label}{state}")
        if cs.has_statement:
            out.append(f"  пришло {rub(cs.total_in)} · ушло {rub(cs.total_out)}")
            out.append(f"  бизнес {rub(cs.business)} · личное {rub(cs.personal)}")
        else:
            out.append(f"  бизнес {rub(cs.business)}")
        if cs.missing:
            out.append(f"  ⚠️ не найдено в выписке: {len(cs.missing)}")
    out.append("")
    total_in, total_out, personal = s.total("total_in"), s.total("total_out"), s.total("personal")
    if total_out is not None:
        out.append(f"Всего по картам с выпиской: пришло {rub(total_in)}, ушло {rub(total_out)}")
        out.append(f"  из них личное: {rub(personal)}")
    out.append(f"Бизнес-расходы с личных карт: {rub(s.business)}")
    for name, amount in s.by_category.items():
        out.append(f"  • {name}: {rub(amount)}")
    if s.reimbursed:
        out.append(f"Бизнес уже вернул: {rub(s.reimbursed)}")
    out.append(f"💰 Бизнес должен вам за месяц: {rub(s.owed)}")
    if owed_total is not None and owed_total != s.owed:
        out.append(f"💰 С начала учёта по конец месяца: {rub(owed_total)}")
    return "\n".join(out)


def _period_name(summaries: list[MonthSummary]) -> str:
    return f"{month_name(summaries[0].month)} — {month_name(summaries[-1].month)}"


def period_text(summaries: list[MonthSummary], owed_total: int) -> str:
    out = [f"📊 Свод: {_period_name(summaries)}", "",
           "По месяцам (бизнес · личное по выпискам · возмещено):"]
    for s in summaries:
        personal = s.total("personal")
        no_stmt = [c.card.name for c in s.cards if not c.has_statement]
        gap = f"  (нет выписки: {', '.join(no_stmt)})" if no_stmt and len(no_stmt) < len(s.cards) else ""
        if len(no_stmt) == len(s.cards):
            gap = "  (выписок нет)"
        out.append(f"  {_short(s.month)}: {rub(s.business)} · {rub(personal)} · {rub(s.reimbursed)}{gap}")
    out.append("")
    out.append("По картам за период:")
    for i, cs0 in enumerate(summaries[0].cards):
        per = [s.cards[i] for s in summaries]
        with_stmt = [c for c in per if c.has_statement]
        line = f"  💳 {cs0.card.label}: бизнес {rub(sum(c.business for c in per))}"
        if with_stmt:
            line += (f" · пришло {rub(sum(c.total_in or 0 for c in with_stmt))}"
                     f" · ушло {rub(sum(c.total_out or 0 for c in with_stmt))}"
                     f" · личное {rub(sum(c.personal or 0 for c in with_stmt))}")
        missing = sum(len(c.missing) for c in per)
        if missing:
            line += f" · ⚠️ не найдено в выписках: {missing}"
        out.append(line)
    out.append("")
    business = sum(s.business for s in summaries)
    reimbursed = sum(s.reimbursed for s in summaries)
    out.append(f"Бизнес-расходы с личных карт за период: {rub(business)}")
    for name, amount in _merge_categories(summaries).items():
        out.append(f"  • {name}: {rub(amount)}")
    if reimbursed:
        out.append(f"Бизнес вернул за период: {rub(reimbursed)}")
    out.append(f"💰 Бизнес должен вам на конец периода: {rub(owed_total)}")
    return "\n".join(out)


def _short(month: str) -> str:
    year, mon = month.split("-")
    return f"{MONTHS[int(mon) - 1][:3]} {year[2:]}"


def _merge_categories(summaries: list[MonthSummary]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for s in summaries:
        for name, amount in s.by_category.items():
            merged[name] = merged.get(name, 0) + amount
    return dict(sorted(merged.items(), key=lambda kv: -kv[1]))


def month_xlsx(s: MonthSummary) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Свод"
    bold = Font(bold=True)
    ws.append([f"Свод за {month_name(s.month)}"])
    ws["A1"].font = bold
    ws.append([])
    ws.append(["Карта", "Пришло", "Ушло", "Переводы между своими (ушло)", "Бизнес",
               "Личное", "Возмещено бизнесом", "Не найдено в выписке"])
    for cell in ws[3]:
        cell.font = bold
    for cs in s.cards:
        ws.append([cs.card.label, _num(cs.total_in), _num(cs.total_out), _num(cs.own_out),
                   _num(cs.business), _num(cs.personal), _num(cs.reimbursed), len(cs.missing)])
    ws.append([])
    ws.append(["Бизнес-расходы по статьям"])
    ws.cell(ws.max_row, 1).font = bold
    for name, amount in s.by_category.items():
        ws.append([name, _num(amount)])
    ws.append(["Итого бизнес", _num(s.business)])
    ws.append(["Бизнес уже вернул", _num(s.reimbursed)])
    ws.append(["К возмещению", _num(s.owed)])
    ws.cell(ws.max_row, 1).font = bold
    ws.column_dimensions["A"].width = 34
    for col in "BCDEFGH":
        ws.column_dimensions[col].width = 16

    ops = wb.create_sheet("Бизнес-расходы")
    ops.append(["№", "Дата", "Сумма", "Карта", "Статья", "Получатель", "Что оплачено", "Чек"])
    for cell in ops[1]:
        cell.font = bold
    for e in s.business_expenses:
        ops.append([e.id, e.op_date, _num(e.amount), e.card, e.category or "", e.merchant,
                    e.description, e.receipt_path])
    for col, width in zip("ABCDEFGH", (6, 12, 14, 18, 30, 26, 40, 30)):
        ops.column_dimensions[col].width = width

    missing = [e for cs in s.cards for e in cs.missing]
    if missing:
        m = wb.create_sheet("Не найдено в выписке")
        m.append(["№", "Дата", "Сумма", "Карта", "Получатель"])
        for e in missing:
            m.append([e.id, e.op_date, _num(e.amount), e.card, e.merchant])

    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, float):
                    cell.number_format = "# ##0.00"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _num(kopecks: int | None):
    return None if kopecks is None else kopecks / 100


def period_xlsx(summaries: list[MonthSummary], owed_total: int) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    bold = Font(bold=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "По месяцам"
    ws.append([f"Свод: {_period_name(summaries)}"])
    ws["A1"].font = bold
    ws.append([])
    ws.append(["Месяц", "Карта", "Пришло", "Ушло", "Переводы между своими (ушло)", "Бизнес",
               "Личное", "Возмещено бизнесом", "Не найдено в выписке"])
    for cell in ws[3]:
        cell.font = bold
    for s in summaries:
        for cs in s.cards:
            ws.append([s.month, cs.card.label, _num(cs.total_in), _num(cs.total_out),
                       _num(cs.own_out), _num(cs.business), _num(cs.personal),
                       _num(cs.reimbursed), len(cs.missing)])
    ws.append([])
    ws.append(["Бизнес за период", "", "", "", "", _num(sum(s.business for s in summaries))])
    ws.append(["Возмещено за период", "", "", "", "", "", "",
               _num(sum(s.reimbursed for s in summaries))])
    ws.append(["К возмещению на конец периода", "", "", "", "", _num(owed_total)])
    ws.cell(ws.max_row, 1).font = bold
    for col, width in zip("ABCDEFGHI", (30, 18, 14, 14, 16, 14, 14, 16, 12)):
        ws.column_dimensions[col].width = width

    cats = wb.create_sheet("По статьям")
    months = [s.month for s in summaries]
    cats.append(["Статья"] + months + ["Итого"])
    for cell in cats[1]:
        cell.font = bold
    for name, total in _merge_categories(summaries).items():
        cats.append([name] + [_num(s.by_category.get(name, 0)) for s in summaries] + [_num(total)])
    cats.column_dimensions["A"].width = 34

    ops = wb.create_sheet("Бизнес-расходы")
    ops.append(["№", "Дата", "Сумма", "Карта", "Статья", "Получатель", "Что оплачено", "Чек"])
    for cell in ops[1]:
        cell.font = bold
    for s in summaries:
        for e in s.business_expenses:
            ops.append([e.id, e.op_date, _num(e.amount), e.card, e.category or "", e.merchant,
                        e.description, e.receipt_path])
    for col, width in zip("ABCDEFGH", (6, 12, 14, 18, 30, 26, 40, 30)):
        ops.column_dimensions[col].width = width

    missing = [(s.month, e) for s in summaries for cs in s.cards for e in cs.missing]
    if missing:
        m = wb.create_sheet("Не найдено в выписке")
        m.append(["№", "Дата", "Сумма", "Карта", "Получатель"])
        for _, e in missing:
            m.append([e.id, e.op_date, _num(e.amount), e.card, e.merchant])

    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, float):
                    cell.number_format = "# ##0.00"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
