"""Сверка записанных операций с выпиской и месячный свод.

Итог месяца — две суммы: сколько ушло на бизнес (с разбивкой по статьям) и
сколько на личные расходы. Переводы между своими счетами (в том числе с
бизнес-счёта себе) — не расход ни того, ни другого.
"""

from dataclasses import dataclass, field
from datetime import date

from .storage import (BUSINESS, EXPENSE, PERSONAL, Card, Expense, StatementLine, Storage,
                      Transfer)

# Банк может провести оплату на пару дней позже, чем она видна на скриншоте.
MATCH_DAYS = 3
NO_CATEGORY = "Без статьи"


def match(expenses: list, lines: list[StatementLine], direction: str = "out",
          used: set[int] | None = None) -> tuple[dict[int, int], list]:
    """Сопоставить записи строкам выписки: сумма точно, дата ±MATCH_DAYS.

    Годится и для расходов, и для переводов (у тех и других есть id, op_date,
    amount). Возвращает {id записи: id строки} и записи, не найденные в выписке.
    Каждая строка используется не больше одного раза — в том числе между
    вызовами, если передан общий `used`.
    """
    used = set() if used is None else used
    pairs: dict[int, int] = {}
    missing: list = []
    for e in sorted(expenses, key=lambda x: (x.op_date, x.id)):
        e_date = date.fromisoformat(e.op_date)
        best = None
        for ln in lines:
            if ln.id in used or ln.direction != direction or ln.amount != e.amount:
                continue
            gap = abs((date.fromisoformat(ln.op_date) - e_date).days)
            if gap <= MATCH_DAYS and (best is None or gap < best[0]):
                best = (gap, ln.id)
        if best:
            used.add(best[1])
            pairs[e.id] = best[1]
        else:
            missing.append(e)
    return pairs, missing


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
    personal_marked: int = 0         # записи, которые вы пометили как личные
    by_category: dict[str, int] = field(default_factory=dict)
    missing: list[Expense] = field(default_factory=list)
    unmatched_out: list[StatementLine] = field(default_factory=list)

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
        """Ушло на личное; None — если по личной карте нет выписки (остаток неизвестен)."""
        if self.card.is_business:
            return self.personal_marked
        if self.total_out is None:
            return None
        return self.net_out - self.business

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
        """Личные расходы по тем картам, где их можно посчитать."""
        return sum(c.personal or 0 for c in self.cards)

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


def summarize(storage: Storage, month: str) -> MonthSummary:
    cards = []
    business_expenses = []
    for card in storage.cards():
        # Старые записи-«возмещения» (прежняя версия) — движение между своими
        # счетами, в расходы не входят.
        expenses = [e for e in storage.expenses(month, card.id) if e.kind == EXPENSE]
        cs = CardSummary(card=card)
        for e in expenses:
            if e.purpose == BUSINESS:
                cs.business_recorded += e.amount
                key = e.category or NO_CATEGORY
                cs.by_category[key] = cs.by_category.get(key, 0) + e.amount
                business_expenses.append(e)
            elif e.purpose == PERSONAL:
                cs.personal_marked += e.amount

        cs.transfers = storage.transfers(month, card.id)
        sent = [t for t in cs.transfers if t.from_card_id == card.id]
        received = [t for t in cs.transfers if t.to_card_id == card.id]

        found = storage.statement(card.id, month)
        if not found:
            # Без выписки считать нечего, но записанные переводы видны в карточке.
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
            used: set[int] = set()
            pairs, cs.missing = match(expenses, lines, "out", used) if lines else ({}, [])
            # Записанные вами переводы находим в выписке, чтобы не вычесть их
            # дважды — если модель тоже пометила строку как перевод между своими.
            out_pairs, miss_out = match(sent, lines, "out", used) if lines else ({}, [])
            in_pairs, miss_in = match(received, lines, "in", used) if lines else ({}, [])
            if lines:
                cs.missing_transfers = miss_out + miss_in
            as_transfer = set(out_pairs.values()) | set(in_pairs.values())
            flagged = [ln for ln in lines if ln.own_transfer and ln.id not in as_transfer]
            # Записанные переводы вычитаем всегда: по выписке с одними итогами
            # (экран «История» ВТБ) их иначе не увидеть.
            cs.own_in = (sum(ln.amount for ln in flagged if ln.direction == "in")
                         + sum(t.amount for t in received))
            cs.own_out = (sum(ln.amount for ln in flagged if ln.direction == "out")
                          + sum(t.amount for t in sent))
            if lines:
                matched = set(pairs.values()) | as_transfer
                cs.unmatched_out = [
                    ln for ln in lines
                    if ln.direction == "out" and not ln.own_transfer and ln.id not in matched
                ]
        cards.append(cs)
    return MonthSummary(month, cards, business_expenses)
