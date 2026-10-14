"""Сверка записанных операций с выпиской и месячный свод.

Итог месяца — две суммы: сколько ушло на бизнес (с разбивкой по статьям) и
сколько на личные расходы. Переводы между своими счетами (в том числе с
бизнес-счёта себе) — не расход ни того, ни другого.

Сопоставление записей со строками выписки идёт по карте целиком, а не внутри
одного месяца: оплата 30.09 может стоять в выписке 01.10. Сопоставленная
запись относится к месяцу своей строки выписки («месяц банка») — и в /itog,
и в /svod, поэтому их итоги совпадают.
"""

from dataclasses import dataclass, field
from datetime import date

from .storage import (BUSINESS, EXPENSE, PERSONAL, Card, Expense, StatementLine, Storage,
                      Transfer)

# Банк может провести оплату на пару дней позже, чем она видна на скриншоте.
MATCH_DAYS = 3
# Месяц выписки «только итоги»: в соседний месяц запись уводим, только если
# банк провёл её на 1–2 дня позже (30.09 → 01.10), а не дальше.
BOUNDARY_DAYS = 2
# Расхождение итога и суммы строк, при котором строки ещё считаются полными
# (операция «в обработке», округление, комиссия внутри операции).
COMPLETE_TOLERANCE = 50_000  # 500 ₽
NO_CATEGORY = "Без статьи"


def _gap(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def match(records: list, lines: list[StatementLine], direction: str = "out",
          used: set[int] | None = None, prefer_own: bool = False,
          closed_months: set[str] = frozenset()) -> tuple[dict[int, int], list]:
    """Сопоставить записи строкам выписки: сумма точно, дата ±MATCH_DAYS.

    Годится и для расходов, и для переводов (у тех и других есть id, op_date,
    amount). Возвращает {id записи: id строки} и записи, не найденные в выписке.
    Каждая строка используется не больше одного раза — в том числе между
    вызовами, если передан общий `used`.

    Не жадно по порядку дат: сначала записи с наименьшим числом подходящих
    строк, чтобы ранняя запись не забрала строку, единственную для соседней.
    prefer_own — для переводов предпочитать строки, которые модель пометила как
    «свой перевод»; для расходов — наоборот, обычные строки.
    closed_months — месяцы, где у карты выписка только с итогами (без строк):
    запись такого месяца не уводим в строку соседнего месяца — по итогам не
    проверить, что её там нет, а строка соседнего месяца — скорее другая покупка.
    """
    used = set() if used is None else used

    def candidates(r):
        return [ln for ln in lines
                if ln.id not in used and ln.direction == direction and ln.amount == r.amount
                and _gap(ln.op_date, r.op_date) <= MATCH_DAYS
                and (ln.op_date[:7] == r.op_date[:7] or r.op_date[:7] not in closed_months
                     # 30.09 → 01.10: банк провёл на следующий день — это та же оплата.
                     or _gap(ln.op_date, r.op_date) <= BOUNDARY_DAYS)]

    pairs: dict[int, int] = {}
    pending = sorted(records, key=lambda r: (r.op_date, r.id))
    while pending:
        pending.sort(key=lambda r: (len(candidates(r)), r.op_date, r.id))
        r = pending.pop(0)
        options = candidates(r)
        if not options:
            continue
        best = min(options, key=lambda ln: (ln.own_transfer != prefer_own,
                                            ln.op_date[:7] != r.op_date[:7],
                                            _gap(ln.op_date, r.op_date), ln.op_date, ln.id))
        used.add(best.id)
        pairs[r.id] = best.id
    missing = [r for r in records if r.id not in pairs]
    return pairs, missing


@dataclass
class CardMatching:
    """Сопоставление всех записей карты со всеми её выписками."""
    lines: dict[int, StatementLine]
    expense_line: dict[int, int]          # id расхода → id строки
    sent_line: dict[int, int]             # id перевода (с этой карты) → id строки
    received_line: dict[int, int]         # id перевода (на эту карту) → id строки
    statement_months: set[str]

    @property
    def used(self) -> set[int]:
        return (set(self.expense_line.values()) | set(self.sent_line.values())
                | set(self.received_line.values()))

    def month_of(self, record_id: int, pairs: dict[int, int], own_date: str) -> str:
        """Месяц банка: по строке выписки, если запись нашлась, иначе по своей дате."""
        line_id = pairs.get(record_id)
        return (self.lines[line_id].op_date if line_id else own_date)[:7]

    def date_of(self, record_id: int, pairs: dict[int, int], own_date: str) -> str:
        line_id = pairs.get(record_id)
        return self.lines[line_id].op_date if line_id else own_date


def match_card(storage: Storage, card: Card) -> CardMatching:
    lines = storage.card_lines(card.id)
    expenses = [e for e in storage.card_expenses(card.id) if e.kind == EXPENSE]
    transfers = storage.card_transfers(card.id)
    sent = [t for t in transfers if t.from_card_id == card.id]
    received = [t for t in transfers if t.to_card_id == card.id]
    used: set[int] = set()
    # Сначала переводы (им подходят строки, помеченные как «свои»), потом расходы
    # (им — обычные строки): иначе расход мог забрать строку перевода, и сумма
    # вычиталась бы дважды.
    months = {m for (m,) in storage.conn.execute(
        "SELECT month FROM statements WHERE card_id = ?", (card.id,))}
    line_months = {ln.op_date[:7] for ln in lines}
    closed = {m for m in months if m not in line_months}   # выписка только с итогами
    sent_pairs, _ = match(sent, lines, "out", used, prefer_own=True, closed_months=closed)
    received_pairs, _ = match(received, lines, "in", used, prefer_own=True, closed_months=closed)
    expense_pairs, _ = match(expenses, lines, "out", used, prefer_own=False, closed_months=closed)
    return CardMatching({ln.id: ln for ln in lines}, expense_pairs, sent_pairs,
                        received_pairs, months)


@dataclass
class CardSummary:
    card: Card
    has_statement: bool = False
    lines_checked: bool = False      # были ли в выписке построчные операции
    total_in: int | None = None
    total_out: int | None = None
    own_in: int = 0                  # переводы со своих счетов
    own_out: int = 0                 # переводы на свои счета (с бизнес-счёта — вам)
    transfers: list[Transfer] = field(default_factory=list)          # записанные вами
    missing_transfers: list[Transfer] = field(default_factory=list)  # не найдены в выписке
    business_recorded: int = 0       # записанные бизнес-расходы
    business_unconfirmed: int = 0    # из них не найдены в построчной выписке
    personal_marked: int = 0         # записи, которые вы пометили как личные
    by_category: dict[str, int] = field(default_factory=dict)
    missing: list[Expense] = field(default_factory=list)
    unmatched_out: list[StatementLine] = field(default_factory=list)
    effective_dates: dict[int, str] = field(default_factory=dict)  # расход → дата банка

    @property
    def net_in(self) -> int | None:
        """Пришло без переводов со своих счетов."""
        return None if self.total_in is None else self.total_in - self.own_in

    @property
    def net_out(self) -> int | None:
        """Ушло без переводов на свои счета — то, что реально потрачено."""
        return None if self.total_out is None else self.total_out - self.own_out

    @property
    def business(self) -> int:
        """Ушло на бизнес.

        Личная карта — то, что вы записали как бизнес. Бизнес-счёт с выпиской —
        всё, что с него ушло, кроме переводов вам и отмеченного как личное:
        это деньги бизнеса, даже если статья ещё не проставлена.
        """
        if self.card.is_business and self.total_out is not None:
            return max(self.net_out - self.personal_marked, self.business_recorded)
        return self.business_recorded

    @property
    def personal(self) -> int | None:
        """Ушло на личное; None — если по личной карте нет выписки (остаток неизвестен).

        Бизнес-запись, которой нет в построчной выписке (скорее всего выбрана не
        та карта), из личного не вычитается — иначе личное могло бы уйти в минус.
        Отрицательный остаток (выписка только с итогами) показывается как 0,
        а расхождение — предупреждением (overbooked).
        """
        if self.card.is_business:
            return self.personal_marked
        if self.total_out is None:
            return None
        return max(self.net_out - (self.business - self.business_unconfirmed), 0)

    @property
    def overbooked(self) -> int:
        """На сколько записанное превышает то, что по выписке ушло с карты."""
        if self.total_out is None:
            return 0
        spent = self.net_out - (0 if not self.card.is_business else self.personal_marked)
        booked = self.business_recorded - self.business_unconfirmed
        return max(booked - spent, 0)

    @property
    def categories(self) -> dict[str, int]:
        """Бизнес по статьям; неразнесённый остаток бизнес-счёта — «Без статьи»."""
        result = dict(self.by_category)
        rest = self.business - self.business_recorded
        if rest:
            result[NO_CATEGORY] = result.get(NO_CATEGORY, 0) + rest
        return result


@dataclass
class MonthSummary:
    month: str
    cards: list[CardSummary]
    business_expenses: list[Expense]

    @property
    def business(self) -> int:
        return sum(c.business for c in self.cards)

    @property
    def net_in(self) -> int | None:
        """Приход по картам с выпиской без переводов между своими счетами.

        Перевод с одного своего счёта на другой уходит из итога: он не
        появляется ни в приходе, ни в расходе.
        """
        values = [c.net_in for c in self.cards if c.net_in is not None]
        return sum(values) if values else None

    @property
    def personal(self) -> int:
        """Личные расходы по тем картам, где их можно посчитать; по карте без
        выписки — хотя бы то, что вы сами отметили как личное."""
        return sum(c.personal if c.personal is not None else c.personal_marked
                   for c in self.cards)

    @property
    def personal_incomplete(self) -> list[Card]:
        """Личные карты без выписки: их личные расходы в итог не попали."""
        return [c.card for c in self.cards if c.personal is None]

    @property
    def by_category(self) -> dict[str, int]:
        merged: dict[str, int] = {}
        for c in self.cards:
            for name, amount in c.categories.items():
                merged[name] = merged.get(name, 0) + amount
        return dict(sorted(merged.items(), key=lambda kv: -kv[1]))

    @property
    def personal_cards(self) -> list[CardSummary]:
        return [c for c in self.cards if not c.card.is_business]

    @property
    def business_accounts(self) -> list[CardSummary]:
        return [c for c in self.cards if c.card.is_business]


def summarize(storage: Storage, month: str, cache: dict | None = None) -> MonthSummary:
    """Свод месяца. cache — {card_id: CardMatching}, чтобы не сопоставлять заново
    для каждого месяца периода (/svod, /itog за год)."""
    cards = []
    business_expenses = []
    for card in storage.cards():
        if cache is not None and card.id in cache:
            m = cache[card.id]
        else:
            m = match_card(storage, card)
            if cache is not None:
                cache[card.id] = m
        cs = CardSummary(card=card)

        # Расходы месяца — по месяцу банка. Старые записи-«возмещения» (прежняя
        # версия) — движение между своими счетами, в расходы не входят.
        expenses = [e for e in storage.card_expenses(card.id) if e.kind == EXPENSE
                    and m.month_of(e.id, m.expense_line, e.op_date) == month]
        lines_complete = False
        found = storage.statement(card.id, month)
        if found:
            head, lines = found
            # Строки считаем полными, если итога нет или он сходится с суммой строк.
            # Экран «История» банка даёт итог месяца и только видимые строки:
            # расход, которого среди них нет, ещё не значит «не та карта».
            sum_out_lines = sum(ln.amount for ln in lines if ln.direction == "out")
            lines_complete = bool(lines) and (
                head["total_out"] is None
                or abs(head["total_out"] - sum_out_lines)
                <= max(COMPLETE_TOLERANCE, head["total_out"] // 50))  # до 2% итога
        for e in expenses:
            cs.effective_dates[e.id] = m.date_of(e.id, m.expense_line, e.op_date)
            unconfirmed = lines_complete and e.id not in m.expense_line
            if unconfirmed:
                cs.missing.append(e)
            if e.purpose == BUSINESS:
                cs.business_recorded += e.amount
                if unconfirmed:
                    cs.business_unconfirmed += e.amount
                key = e.category or NO_CATEGORY
                cs.by_category[key] = cs.by_category.get(key, 0) + e.amount
                business_expenses.append(e)
            elif e.purpose == PERSONAL:
                cs.personal_marked += e.amount

        all_transfers = storage.card_transfers(card.id)
        sent = [t for t in all_transfers if t.from_card_id == card.id
                and m.month_of(t.id, m.sent_line, t.op_date) == month]
        received = [t for t in all_transfers if t.to_card_id == card.id
                    and m.month_of(t.id, m.received_line, t.op_date) == month]
        cs.transfers = sorted({t.id: t for t in sent + received}.values(), key=lambda t: t.op_date)
        cs.own_out = sum(t.amount for t in sent)
        cs.own_in = sum(t.amount for t in received)

        if found:
            head, lines = found
            cs.has_statement = True
            cs.lines_checked = bool(lines)
            sum_in = sum(ln.amount for ln in lines if ln.direction == "in")
            sum_out = sum(ln.amount for ln in lines if ln.direction == "out")
            # Итоги, напечатанные в выписке, точнее суммы распознанных строк
            # (строку модель может пропустить, итог — вряд ли).
            cs.total_in = head["total_in"] if head["total_in"] is not None else sum_in
            cs.total_out = head["total_out"] if head["total_out"] is not None else sum_out
            used = m.used
            # Строки, которые модель пометила как «свой перевод», но которые не
            # заняты ни записанным переводом, ни расходом, — тоже переводы.
            flagged = [ln for ln in lines if ln.own_transfer and ln.id not in used]
            missing_sent = [t for t in sent if t.id not in m.sent_line]
            missing_received = [t for t in received if t.id not in m.received_line]
            # Записанный перевод, не найденный по дате (разрыв больше 3 дней), и
            # помеченная строка той же суммы — это один перевод: не вычитаем дважды.
            for t, direction in ([(t, "out") for t in missing_sent]
                                 + [(t, "in") for t in missing_received]):
                twin = next((ln for ln in flagged
                             if ln.direction == direction and ln.amount == t.amount), None)
                if twin is not None:
                    flagged.remove(twin)
            cs.own_in += sum(ln.amount for ln in flagged if ln.direction == "in")
            cs.own_out += sum(ln.amount for ln in flagged if ln.direction == "out")
            if lines:
                cs.missing_transfers = missing_sent + missing_received
                cs.unmatched_out = [ln for ln in lines if ln.direction == "out"
                                    and not ln.own_transfer and ln.id not in used]
        cards.append(cs)
    return MonthSummary(month, cards, business_expenses)
