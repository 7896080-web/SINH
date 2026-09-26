"""Сверка записанных операций с выпиской и месячный свод."""

from dataclasses import dataclass, field
from datetime import date

from .storage import (BUSINESS, BUSINESS_ACCOUNT, REIMBURSEMENT, Card, Expense, StatementLine,
                      Storage)

# Банк может провести оплату на пару дней позже, чем она видна на скриншоте.
MATCH_DAYS = 3


def match(expenses: list[Expense], lines: list[StatementLine]) -> tuple[dict[int, int], list[Expense]]:
    """Сопоставить записи строкам выписки: сумма точно, дата ±MATCH_DAYS.

    Возвращает {expense_id: line_id} и список записей, не найденных в выписке.
    Каждая строка выписки используется не больше одного раза.
    """
    used: set[int] = set()
    pairs: dict[int, int] = {}
    missing: list[Expense] = []
    for e in sorted(expenses, key=lambda x: (x.op_date, x.id)):
        # Возмещение на личной карте — зачисление; на бизнес-счёте это перевод
        # владельцу, то есть списание. Любой расход — списание.
        incoming = e.kind == REIMBURSEMENT and e.card_kind != BUSINESS_ACCOUNT
        want = "in" if incoming else "out"
        e_date = date.fromisoformat(e.op_date)
        best = None
        for ln in lines:
            if ln.id in used or ln.direction != want or ln.amount != e.amount:
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
    own_in: int = 0                  # переводы со своих карт
    own_out: int = 0                 # переводы на свои карты
    business: int = 0                # бизнес-расходы, оплаченные с этой карты
    reimbursed: int = 0              # личная карта: возмещения на неё; бизнес-счёт: переводы вам
    personal_marked: int = 0         # расходы, которые вы сами пометили как личные
    missing: list[Expense] = field(default_factory=list)
    unmatched_out: list[StatementLine] = field(default_factory=list)
    unmatched_own_out: list[StatementLine] = field(default_factory=list)  # бизнес-счёт → вам

    @property
    def personal(self) -> int | None:
        """Всё, что ушло с личной карты, минус бизнес и переводы между своими счетами.

        Для бизнес-счёта не считается: всё, что с него ушло, — деньги бизнеса.
        """
        if self.total_out is None or self.card.is_business:
            return None
        return self.total_out - self.own_out - self.business


@dataclass
class MonthSummary:
    month: str
    cards: list[CardSummary]
    by_category: dict[str, int]      # все бизнес-расходы: с личных карт и с бизнес-счёта
    business_expenses: list[Expense]

    @property
    def personal_cards(self) -> list[CardSummary]:
        return [c for c in self.cards if not c.card.is_business]

    @property
    def business_accounts(self) -> list[CardSummary]:
        return [c for c in self.cards if c.card.is_business]

    @property
    def business(self) -> int:
        """Бизнес-расходы, оплаченные с личных карт (их бизнес должен вернуть)."""
        return sum(c.business for c in self.personal_cards)

    @property
    def business_account_spent(self) -> int:
        return sum(c.business for c in self.business_accounts)

    @property
    def reimbursed(self) -> int:
        """Сколько владелец получил от бизнеса: возмещения и личные траты с бизнес-счёта."""
        return (sum(c.reimbursed for c in self.cards)
                + sum(c.personal_marked for c in self.business_accounts))

    @property
    def owed(self) -> int:
        """Сколько бизнес должен вернуть владельцу за этот месяц."""
        return self.business - self.reimbursed

    def total(self, attr: str) -> int | None:
        """Сумма по личным картам с выпиской (бизнес-счёт в «личное» не входит)."""
        values = [getattr(c, attr) for c in self.personal_cards if c.has_statement]
        return sum(v for v in values if v is not None) if values else None


def summarize(storage: Storage, month: str) -> MonthSummary:
    cards = []
    by_category: dict[str, int] = {}
    business_expenses = []
    for card in storage.cards():
        expenses = storage.expenses(month, card.id)
        cs = CardSummary(card=card)
        for e in expenses:
            if e.kind == REIMBURSEMENT:
                cs.reimbursed += e.amount
            elif e.purpose == BUSINESS:
                cs.business += e.amount
                business_expenses.append(e)
                key = e.category or "Без статьи"
                by_category[key] = by_category.get(key, 0) + e.amount
            else:
                cs.personal_marked += e.amount

        found = storage.statement(card.id, month)
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
            cs.own_in = sum(ln.amount for ln in lines if ln.own_transfer and ln.direction == "in")
            cs.own_out = sum(ln.amount for ln in lines if ln.own_transfer and ln.direction == "out")
            if lines:
                pairs, cs.missing = match(expenses, lines)
                matched = set(pairs.values())
                cs.unmatched_out = [
                    ln for ln in lines
                    if ln.direction == "out" and not ln.own_transfer and ln.id not in matched
                ]
                if card.is_business:
                    cs.unmatched_own_out = [
                        ln for ln in lines
                        if ln.direction == "out" and ln.own_transfer and ln.id not in matched
                    ]
        cards.append(cs)
    by_category = dict(sorted(by_category.items(), key=lambda kv: -kv[1]))
    return MonthSummary(month, cards, by_category, business_expenses)
