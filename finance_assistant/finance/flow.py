"""Логика диалога — без Telegram. bot.py только переводит апдейты в вызовы Flow
и отправляет ответы, поэтому весь сценарий проверяется тестами без сети.

Состояние чата (Storage.get_state):
  drafts     — очередь нераспознанных до конца операций; спрашиваем про первую;
  ask        — что сейчас спрашиваем про первую операцию;
  statement  — {"card_id", "month"}, пока идёт загрузка выписки;
  sverka     — {"month"} между выбором месяца и выбором карты.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .money import parse_amount
from .recognize import RecognitionError
from .report import (MONTHS, card_text, expense_card, expense_line, month_name, month_text,
                     month_xlsx, period_text, period_xlsx, rub, suggestions_text,
                     unmatched_text)
from .reconcile import summarize
from .storage import BUSINESS, EXPENSE, PERSONAL, REIMBURSEMENT, Storage

log = logging.getLogger(__name__)

HELP = """\
Я веду учёт расходов на бизнес, оплаченных с личных карт.

📸 Сделали оплату — пришлите скриншот (можно с подписью, что это). \
Я распознаю сумму, дату, карту и статью, при необходимости переспрошу и запишу.
✍️ Можно и текстом: «3500 доставка СДЭК с Т-Банка вчера».

В конце месяца:
/sverka — загрузить выписку по карте (PDF, скриншоты, Excel/CSV или просто \
«пришло 120000, ушло 95000»). Можно за любой из последних 12 месяцев \
или одну выписку сразу за несколько месяцев — так удобно начать учёт задним числом.
После выписки бот предложит списания, похожие на бизнес: записать все одной \
кнопкой, лишние убрать /notbiz 12 15, недостающие добавить /biz 7 9.
/itog — свод за месяц (/itog 2026-03), за год (/itog 2026) или период \
(/itog 2026-01..2026-06): пришло/ушло, бизнес/личное, по статьям, \
сколько бизнес должен вернуть; плюс Excel
/vypiska 2026-03 — списания месяца, которые сейчас считаются личными

Прочее:
/list — записи за месяц (/list 2026-02) · /fix 42 — исправить запись
/cards — мои карты · /addcard Название 1234 Банк · /delcard N
/cats — статьи · /addcat Название
/cancel — сбросить текущий вопрос"""

MONTH_ARG_HELP = "Месяц указывайте как 2026-09"
PICKER_MONTHS = 12   # сколько месяцев назад можно выбрать кнопкой в /sverka
PERIOD = "period"    # «выписка за несколько месяцев»


@dataclass
class Reply:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    file: tuple[str, bytes] | None = None  # (имя файла, содержимое)


def _month(today: date, shift: int = 0) -> str:
    index = today.year * 12 + today.month - 1 + shift
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _parse_month_arg(arg: str) -> str | None:
    try:
        return datetime.strptime(arg.strip(), "%Y-%m").strftime("%Y-%m")
    except ValueError:
        return None


def _short_month(month: str) -> str:
    year, mon = month.split("-")
    return f"{MONTHS[int(mon) - 1][:3].capitalize()} {year[2:]}"


def _months_between(first: str, last: str) -> list[str]:
    months, m = [], first
    while m <= last:
        months.append(m)
        y, mo = (int(x) for x in m.split("-"))
        m = f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"
    return months


def _parse_period_arg(arg: str, today: date) -> tuple[str, str] | None:
    """'2026-09' | '2026' (год, но не дальше текущего месяца) | '2026-01..2026-09'."""
    arg = arg.strip().replace(" ", "")
    if ".." in arg:
        first, _, last = arg.partition("..")
        first, last = _parse_month_arg(first), _parse_month_arg(last)
        return (first, last) if first and last and first <= last else None
    if arg.isdigit() and len(arg) == 4:
        first, last = f"{arg}-01", min(f"{arg}-12", _month(today))
        return (first, last) if first <= last else None
    month = _parse_month_arg(arg)
    return (month, month) if month else None


def _parse_date(text: str, today: date) -> str | None:
    text = text.strip().lower()
    if text == "сегодня":
        return today.isoformat()
    if text == "вчера":
        return (today - timedelta(days=1)).isoformat()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:  # "05.09" — текущий год, а если дата в будущем — прошлый
        d = datetime.strptime(text, "%d.%m").date().replace(year=today.year)
        return (d if d <= today else d.replace(year=today.year - 1)).isoformat()
    except ValueError:
        return None


def _amount_or_none(text: str) -> int | None:
    try:
        value = parse_amount(text) if text else None
    except ValueError:
        return None
    return value if value and value > 0 else None


class Flow:
    def __init__(self, storage: Storage, recognizer, receipts_dir: str, today=date.today):
        self.db = storage
        self.recognizer = recognizer
        self.receipts_dir = receipts_dir
        self.today = today

    # --- входящие сообщения --------------------------------------------

    def on_command(self, chat_id: int, command: str, arg: str = "") -> list[Reply]:
        handler = {
            "start": self._help, "help": self._help, "cancel": self._cancel,
            "cards": self._cards, "addcard": self._add_card, "delcard": self._del_card,
            "cats": self._cats, "addcat": self._add_cat, "list": self._list,
            "sverka": self._sverka, "done": self._done, "itog": self._itog, "biz": self._biz,
            "notbiz": self._notbiz, "vypiska": self._vypiska, "fix": self._fix,
        }.get(command)
        if not handler:
            return [Reply("Не знаю такой команды. /help — что я умею.")]
        return handler(chat_id, arg.strip())

    def on_files(self, chat_id: int, files: list[tuple[bytes, str]], caption: str = "",
                 filename: str = "") -> list[Reply]:
        """Фото или документ. В режиме сверки — часть выписки, иначе — платёж."""
        state = self.db.get_state(chat_id)
        if "statement" in state:
            return self._statement_input(chat_id, state, files, caption)
        if any(mime not in ("image/jpeg", "image/png", "image/webp") for _, mime in files):
            return [Reply("Похоже на выписку. Чтобы загрузить её для сверки, "
                          "сначала отправьте /sverka.")]
        receipt = self._save_receipt(files[0][0], files[0][1])
        return self._recognize_payment(chat_id, files, caption, receipt)

    def on_text(self, chat_id: int, text: str) -> list[Reply]:
        state = self.db.get_state(chat_id)
        ask = state.get("ask")
        if state.get("drafts") and ask in ("amount", "date"):
            return self._answer_text(chat_id, state, ask, text)
        if "statement" in state:
            return self._statement_input(chat_id, state, [], text)
        if not any(ch.isdigit() for ch in text):
            return [Reply("Пришлите скриншот оплаты или напишите расход текстом, "
                          "например: «3500 доставка СДЭК вчера». /help — подробнее.")]
        return self._recognize_payment(chat_id, [], text, "")

    def on_button(self, chat_id: int, data: str) -> list[Reply]:
        kind, _, rest = data.partition(":")
        if kind == "d":
            return self._answer_button(chat_id, rest)
        if kind == "e":
            return self._edit_saved(chat_id, rest)
        if kind == "s":
            return self._sverka_button(chat_id, rest)
        return []

    # --- распознавание платежа -----------------------------------------

    def _save_receipt(self, data: bytes, mime: str) -> str:
        ext = {"image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
        folder = os.path.join(self.receipts_dir, self.today().strftime("%Y-%m"))
        os.makedirs(folder, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "." + ext
        path = os.path.join(folder, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _recognize_payment(self, chat_id, files, text, receipt) -> list[Reply]:
        cards = self.db.cards()
        categories = [c["name"] for c in self.db.categories()]
        try:
            info = self.recognizer.recognize_payment(
                files, text, today=self.today().isoformat(), cards=cards, categories=categories
            )
        except RecognitionError as exc:
            return [Reply(f"Не получилось распознать: {exc}.")]
        if not info.get("is_payment"):
            return [Reply("Не вижу здесь оплаты или перевода. Пришлите скриншот операции "
                          "из банка или чек, либо напишите сумму текстом.")]

        currency = (info.get("currency") or "RUB").upper()
        amount = _amount_or_none(info.get("amount", ""))
        note = ""
        if currency not in ("RUB", "RUR", "₽"):
            note = f"Операция в {currency} {info.get('amount')}. "
            amount = None  # спросим, сколько списали в рублях
        draft = {
            "amount": amount,
            "date": _parse_date(info.get("date") or "", self.today()),
            "card_id": None,
            "card_hint": (info.get("card_last4") or "", info.get("bank") or ""),
            "kind": EXPENSE if info.get("direction") != "in" else None,
            "purpose": BUSINESS,
            "purpose_asked": not info.get("looks_personal"),
            "category_id": self.db.category_id(info.get("category") or ""),
            "category_confident": bool(info.get("category_confident")),
            "merchant": (info.get("merchant") or "").strip(),
            "description": (info.get("description") or "").strip(),
            "receipt": receipt,
            "note": note,
            "dup_checked": False,
        }
        state = self.db.get_state(chat_id)
        state.setdefault("drafts", []).append(draft)
        self.db.set_state(chat_id, state)
        if len(state["drafts"]) > 1:
            # Сначала доспрашиваем предыдущую операцию, эту — следом.
            return [Reply(f"Принял, разберу после текущей (в очереди: {len(state['drafts']) - 1}).")]
        return self._advance(chat_id)

    @staticmethod
    def _guess_card(cards, last4: str, bank: str) -> int | None:
        last4 = "".join(ch for ch in last4 if ch.isdigit())[-4:]
        if last4:
            hits = [c for c in cards if c.last4 == last4]
            if len(hits) == 1:
                return hits[0].id
        if bank:
            b = bank.lower().replace("-", "").replace(" ", "")
            hits = [c for c in cards if c.bank and (
                c.bank.lower().replace("-", "").replace(" ", "") in b
                or b in c.bank.lower().replace("-", "").replace(" ", ""))]
            if len(hits) == 1:
                return hits[0].id
        if len(cards) == 1:
            return cards[0].id
        return None

    def _advance(self, chat_id: int) -> list[Reply]:
        """Задать следующий нужный вопрос про первую операцию в очереди или сохранить её."""
        replies: list[Reply] = []
        quiet: list[int] = []  # записи из выписки пачкой — одна сводка вместо карточек
        state = self.db.get_state(chat_id)
        while state.get("drafts"):
            d = state["drafts"][0]
            question = self._next_question(d)
            if question:
                state["ask"] = question[0]
                self.db.set_state(chat_id, state)
                return replies + self._quiet_summary(quiet) + [question[1]]
            expense_id = self.db.add_expense(
                op_date=d["date"], amount=d["amount"], card_id=d["card_id"], kind=d["kind"],
                purpose=d["purpose"],
                category_id=d["category_id"] if d["purpose"] == BUSINESS and d["kind"] == EXPENSE else None,
                merchant=d["merchant"], description=d["description"],
                receipt_path=self._file_receipt(d["receipt"], d["date"]),
            )
            self._pop_draft(state)
            self.db.set_state(chat_id, state)
            if d.get("quiet"):
                quiet.append(expense_id)
            else:
                replies.append(self._saved_reply(expense_id))
        return replies + self._quiet_summary(quiet)

    def _quiet_summary(self, ids: list[int]) -> list[Reply]:
        if not ids:
            return []
        items = [self.db.expense(i) for i in ids]
        by_cat: dict[str, int] = {}
        for e in items:
            by_cat[e.category or "Без статьи"] = by_cat.get(e.category or "Без статьи", 0) + e.amount
        lines = [f"✅ Записано как бизнес: {len(items)} на {rub(sum(e.amount for e in items))}"]
        lines += [f"  • {name}: {rub(total)}" for name, total in by_cat.items()]
        lines.append("Поправить отдельную запись: /list месяц, затем /fix номер")
        return [Reply("\n".join(lines))]

    def _file_receipt(self, path: str, op_date: str) -> str:
        """Скриншот лежит в папке месяца, когда его прислали; переносим в месяц оплаты."""
        if not path or not os.path.exists(path):
            return path
        folder = os.path.join(self.receipts_dir, op_date[:7])
        if os.path.dirname(path) == folder:
            return path
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, os.path.basename(path))
        os.replace(path, target)
        return target

    @staticmethod
    def _pop_draft(state: dict):
        state["drafts"].pop(0)
        state.pop("ask", None)
        if not state["drafts"]:
            del state["drafts"]

    def _next_question(self, d: dict):
        head = self._draft_head(d)
        if d["kind"] is None:
            return "kind", Reply(
                head + "Это поступление на карту. Что это?",
                [[("Возмещение от бизнеса", "d:kind:reimb")],
                 [("Не записывать", "d:skip")]])
        if d["amount"] is None:
            return "amount", Reply(head + d.get("note", "") + "Какая сумма списана в рублях? Напишите числом.",
                                   [[("Отмена", "d:skip")]])
        if d["date"] is None:
            return "date", Reply(head + "Не вижу даты. Когда была оплата? Можно написать 05.09.",
                                 [[("Сегодня", "d:date:0"), ("Вчера", "d:date:1")],
                                  [("Отмена", "d:skip")]])
        if d["card_id"] is None:
            cards = self.db.cards()
            # Карты могли добавить, пока висел вопрос, — пробуем угадать заново.
            d["card_id"] = self._guess_card(cards, *d.get("card_hint", ("", "")))
        if d["card_id"] is None:
            if not cards:
                return "card", Reply(
                    "Сначала добавьте свои карты, например:\n"
                    "/addcard Сбер 1234 Сбер\n/addcard Тинькофф 5678 Т-Банк\n"
                    "После этого нажмите «Продолжить».",
                    [[("Продолжить", "d:retry"), ("Отмена", "d:skip")]])
            return "card", Reply(head + "С какой карты оплачено?",
                                 [[(c.label, f"d:card:{c.id}")] for c in cards]
                                 + [[("Отмена", "d:skip")]])
        if d["kind"] == EXPENSE and not d["purpose_asked"]:
            return "purpose", Reply(head + "Похоже на личную покупку. Это расход бизнеса?",
                                    [[("Бизнес", "d:purpose:business"),
                                      ("Личное", "d:purpose:personal")]])
        if (d["kind"] == EXPENSE and d["purpose"] == BUSINESS
                and (d["category_id"] is None or not d["category_confident"])):
            return "category", Reply(head + "Какая статья расходов?",
                                     self._category_buttons("d:cat:", d["category_id"]))
        if not d["dup_checked"]:
            dup = self.db.find_duplicate(op_date=d["date"], amount=d["amount"], card_id=d["card_id"])
            if dup:
                return "dup", Reply(
                    head + f"Такая операция уже записана:\n№{dup.id} {expense_line(dup)}\nЭто повтор?",
                    [[("Повтор, не записывать", "d:skip")],
                     [("Нет, это другая оплата", "d:dup:ok")]])
        return None

    def _draft_head(self, d: dict) -> str:
        parts = []
        if d["amount"]:
            parts.append(rub(d["amount"]))
        if d["date"]:
            parts.append(date.fromisoformat(d["date"]).strftime("%d.%m"))
        who = d["merchant"] or d["description"]
        if who:
            parts.append(who)
        return (" · ".join(parts) + "\n") if parts else ""

    def _category_buttons(self, prefix: str, suggested: int | None) -> list[list[tuple[str, str]]]:
        cats = self.db.categories()
        rows, row = [], []
        for c in cats:
            label = ("✓ " if c["id"] == suggested else "") + c["name"]
            row.append((label, f"{prefix}{c['id']}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    def _answer_text(self, chat_id, state, ask, text) -> list[Reply]:
        d = state["drafts"][0]
        if ask == "amount":
            amount = _amount_or_none(text)
            if amount is None:
                return [Reply("Не понял сумму. Напишите числом, например 1250,50.")]
            d["amount"] = amount
        else:
            parsed = _parse_date(text, self.today())
            if parsed is None:
                return [Reply("Не понял дату. Например: 05.09 или 05.09.2026.")]
            d["date"] = parsed
        self.db.set_state(chat_id, state)
        return self._advance(chat_id)

    def _answer_button(self, chat_id, rest) -> list[Reply]:
        state = self.db.get_state(chat_id)
        if not state.get("drafts"):
            return [Reply("Этот вопрос уже неактуален.")]
        d = state["drafts"][0]
        what, _, value = rest.partition(":")
        if what == "skip":
            self._pop_draft(state)
            self.db.set_state(chat_id, state)
            return [Reply("Не записываю.")] + self._advance(chat_id)
        if what == "kind":
            d["kind"] = REIMBURSEMENT
        elif what == "date":
            d["date"] = (self.today() - timedelta(days=int(value))).isoformat()
        elif what == "card":
            d["card_id"] = int(value)
        elif what == "purpose":
            d["purpose"] = value
            d["purpose_asked"] = True
        elif what == "cat":
            d["category_id"] = int(value)
            d["category_confident"] = True
        elif what == "dup":
            d["dup_checked"] = True
        # "retry" — просто переспросить (например, после /addcard)
        self.db.set_state(chat_id, state)
        return self._advance(chat_id)

    def _saved_reply(self, expense_id: int) -> Reply:
        e = self.db.expense(expense_id)
        buttons = [[("Статья", f"e:cat:{e.id}"), ("Карта", f"e:card:{e.id}")],
                   [("Удалить", f"e:del:{e.id}")]]
        if e.kind == EXPENSE:
            other = ("Это личное", PERSONAL) if e.purpose == BUSINESS else ("Это бизнес", BUSINESS)
            buttons[1].insert(0, (other[0], f"e:purpose:{e.id}:{other[1]}"))
        return Reply(expense_card(e), buttons)

    def _edit_saved(self, chat_id, rest) -> list[Reply]:
        parts = rest.split(":")
        action, expense_id = parts[0], int(parts[1])
        e = self.db.expense(expense_id)
        if not e:
            return [Reply(f"Записи №{expense_id} уже нет.")]
        if action == "del":
            self.db.delete_expense(expense_id)
            return [Reply(f"🗑 Запись №{expense_id} удалена.")]
        if action == "cat" and len(parts) == 2:
            return [Reply(f"Статья для №{e.id}:", self._category_buttons(f"e:cat:{e.id}:", None))]
        if action == "cat":
            self.db.update_expense(expense_id, category_id=int(parts[2]), purpose=BUSINESS)
        elif action == "card" and len(parts) == 2:
            return [Reply(f"Карта для №{e.id}:",
                          [[(c.label, f"e:card:{e.id}:{c.id}")] for c in self.db.cards()])]
        elif action == "card":
            self.db.update_expense(expense_id, card_id=int(parts[2]))
        elif action == "purpose":
            self.db.update_expense(expense_id, purpose=parts[2])
            if parts[2] == BUSINESS and not e.category:
                return [Reply(f"Статья для №{e.id}:", self._category_buttons(f"e:cat:{e.id}:", None))]
        return [self._saved_reply(expense_id)]

    # --- справочники ---------------------------------------------------

    def _help(self, chat_id, arg):
        return [Reply(HELP)]

    def _cancel(self, chat_id, arg):
        self.db.set_state(chat_id, {})
        return [Reply("Сбросил текущий вопрос и режим сверки.")]

    def _cards(self, chat_id, arg):
        cards = self.db.cards()
        if not cards:
            return [Reply("Карт пока нет. Добавьте: /addcard Название 1234 Банк\n"
                          "Например: /addcard Сбер 1234 Сбер")]
        lines = [f"{c.id}. {c.label}" + (f" ({c.bank})" if c.bank else "") for c in cards]
        return [Reply("Ваши карты:\n" + "\n".join(lines))]

    def _add_card(self, chat_id, arg):
        words = arg.split()
        if not words:
            return [Reply("Формат: /addcard Название 1234 Банк — например /addcard Сбер 1234 Сбер")]
        last4 = next((w for w in words if w.isdigit() and len(w) == 4), "")
        rest = [w for w in words if w != last4]
        name = rest[0] if rest else f"Карта {last4}"
        bank = " ".join(rest[1:]) or name
        if any(c.name == name for c in self.db.cards()):
            return [Reply(f"Карта «{name}» уже есть.")]
        card = self.db.add_card(name, last4, bank)
        return [Reply(f"Добавил карту {card.label}.")]

    def _del_card(self, chat_id, arg):
        if not arg.isdigit() or not self.db.card(int(arg)):
            return [Reply("Укажите номер карты из /cards, например /delcard 2")]
        if not self.db.delete_card(int(arg)):
            return [Reply("По этой карте уже есть записи или выписки — удалить нельзя.")]
        return [Reply("Карта удалена.")]

    def _cats(self, chat_id, arg):
        return [Reply("Статьи бизнес-расходов:\n" + "\n".join(
            f"• {c['name']}" for c in self.db.categories()) + "\n\nДобавить: /addcat Название")]

    def _add_cat(self, chat_id, arg):
        if not arg:
            return [Reply("Формат: /addcat Название статьи")]
        self.db.add_category(arg)
        return [Reply(f"Статья «{arg}» есть в списке.")]

    def _fix(self, chat_id, arg):
        if not arg.isdigit() or not self.db.expense(int(arg)):
            return [Reply("Укажите номер записи из /list, например /fix 42")]
        return [self._saved_reply(int(arg))]

    def _list(self, chat_id, arg):
        month = _parse_month_arg(arg) if arg else _month(self.today())
        if not month:
            return [Reply(MONTH_ARG_HELP)]
        items = self.db.expenses(month)
        if not items:
            return [Reply(f"За {month_name(month)} записей нет.")]
        total = sum(e.amount for e in items if e.kind == EXPENSE and e.purpose == BUSINESS)
        lines = [f"№{e.id} {expense_line(e)}" for e in items]
        return [Reply(f"Записи за {month_name(month)}:\n" + "\n".join(lines)
                      + f"\n\nБизнес итого: {rub(total)}")]

    # --- сверка --------------------------------------------------------

    def _sverka(self, chat_id, arg):
        if not self.db.cards():
            return [Reply("Сначала добавьте карты: /addcard Название 1234 Банк")]
        if arg:
            month = _parse_month_arg(arg)
            if not month:
                return [Reply(MONTH_ARG_HELP)]
            return self._ask_card_for_statement(chat_id, month)
        # Последние 12 месяцев — чтобы можно было начать учёт задним числом.
        months = [_month(self.today(), -i) for i in range(PICKER_MONTHS - 1, -1, -1)]
        rows = [[(_short_month(m), f"s:m:{m}") for m in months[i:i + 3]]
                for i in range(0, len(months), 3)]
        rows.append([("📚 Выписка за несколько месяцев", f"s:m:{PERIOD}")])
        return [Reply("За какой месяц сверка?\n"
                      "Если у вас одна выписка сразу за несколько месяцев "
                      "(например, с 1 января), выберите нижнюю кнопку.", rows)]

    def _ask_card_for_statement(self, chat_id, month):
        state = self.db.get_state(chat_id)
        state.pop("statement", None)
        state["sverka"] = {"month": month}
        self.db.set_state(chat_id, state)
        rows = []
        for c in self.db.cards():
            mark = " ✓" if month != PERIOD and self.db.statement(c.id, month) else ""
            rows.append([(c.label + mark, f"s:c:{c.id}")])
        what = "за несколько месяцев" if month == PERIOD else f"за {month_name(month)}"
        return [Reply(f"Сверка {what}. По какой карте выписка?", rows)]

    def _sverka_button(self, chat_id, rest):
        what, _, value = rest.partition(":")
        state = self.db.get_state(chat_id)
        if what == "m":
            return self._ask_card_for_statement(chat_id, value)
        if what == "c" and "sverka" in state:
            month = state.pop("sverka")["month"]
            card = self.db.card(int(value))
            state["statement"] = {"card_id": card.id, "month": None if month == PERIOD else month,
                                  "months": []}
            self.db.set_state(chat_id, state)
            if month == PERIOD:
                return [Reply(
                    f"Жду выписку по карте {card.label} за весь период, например с 1 января "
                    "по сегодня. Присылайте PDF или Excel/CSV из банка, можно несколькими "
                    "файлами. Операции сами разложатся по месяцам.\n"
                    "Когда всё — /done")]
            found = self.db.statement(card.id, month)
            already = ""
            buttons = []
            if found:
                already = f"\nУже загружено строк: {len(found[1])}. Новые добавятся к ним.\n"
                buttons = [[("Загрузить заново", "s:reset")]]
            return [Reply(
                f"Жду выписку по карте {card.label} за {month_name(month)}.{already}\n"
                "Присылайте PDF, скриншоты или Excel/CSV — можно несколькими сообщениями. "
                "Если выписки нет, напишите итоги: «пришло 120000, ушло 95000».\n"
                "Когда всё — /done", buttons)]
        if what == "itog":
            return self._itog(chat_id, value)
        if what == "acc":
            return self._biz(chat_id, "все")
        if what == "reset" and state.get("statement", {}).get("month"):
            st = state["statement"]
            self.db.reset_statement(st["card_id"], st["month"])
            return [Reply("Старые данные выписки удалены, присылайте заново.")]
        return [Reply("Этот вопрос уже неактуален. /sverka — начать сверку.")]

    def _statement_input(self, chat_id, state, files, text):
        st = state["statement"]
        card = self.db.card(st["card_id"])
        period = month_name(st["month"]) if st["month"] else "несколько месяцев"
        try:
            data = self.recognizer.parse_statement(
                files, text, period=period, cards=self.db.cards(),
                categories=[c["name"] for c in self.db.categories()])
        except RecognitionError as exc:
            return [Reply(f"Не получилось разобрать: {exc}.")]
        if not data.get("is_statement"):
            return [Reply("Это не похоже на выписку. Пришлите выписку по карте "
                          f"{card.label} или /done, чтобы закончить.")]
        warn = ""
        last4 = "".join(ch for ch in data.get("card_last4", "") if ch.isdigit())[-4:]
        if last4 and card.last4 and last4 != card.last4:
            warn = f"\n⚠️ В документе карта …{last4}, а сверяем {card.label}. Проверьте."

        by_month: dict[str, list[dict]] = {}
        skipped = 0
        for op in data.get("operations", []):
            amount = _amount_or_none(op.get("amount", "").lstrip("-+"))
            op_date = _parse_date(op.get("date", ""), self.today())
            if amount is None or op_date is None:
                skipped += 1
                continue
            if st["month"] and op_date[:7] != st["month"]:
                skipped += 1  # операции соседнего месяца в выписку этого месяца не берём
                continue
            by_month.setdefault(op_date[:7], []).append({
                "op_date": op_date, "amount": amount, "direction": op["direction"],
                "description": op.get("description", "").strip(),
                "own_transfer": op.get("own_transfer", False),
                "suggested_category": op.get("business_category", "")
                if op["direction"] == "out" and not op.get("own_transfer") else "",
            })

        total_in = _amount_or_none(data.get("total_in", ""))
        total_out = _amount_or_none(data.get("total_out", ""))
        # Напечатанные итоги относим к месяцу, только если документ ровно за один месяц.
        totals_month = st["month"] or (next(iter(by_month)) if len(by_month) == 1 else None)
        if totals_month is None and (total_in is not None or total_out is not None) and not by_month:
            return [Reply("Итоги без выписки можно вносить только за конкретный месяц: "
                          "/done, затем /sverka и выберите месяц.")]
        if totals_month:
            by_month.setdefault(totals_month, [])

        msg, total_added, total_lines = [], 0, 0
        for month in sorted(by_month):
            statement_id = self.db.statement_id(card.id, month)
            added = self.db.add_statement_lines(statement_id, by_month[month])
            if month == totals_month:
                self.db.set_statement_totals(statement_id, total_in, total_out)
            total_added += added
            total_lines += len(by_month[month])
            if month not in st["months"]:
                st["months"].append(month)
            if not st["month"]:
                msg.append(f"  {month_name(month)}: {added}")
        self.db.set_state(chat_id, state)

        repeat = f" (повторы пропущены: {total_lines - total_added})" if total_lines > total_added else ""
        head = [f"Принято операций: {total_added}{repeat}"]
        if skipped:
            head.append(f"Пропущено строк (другой месяц или нечитаемые): {skipped}")
        if totals_month and (total_in is not None or total_out is not None):
            head.append(f"Итоги из документа: пришло {rub(total_in)}, ушло {rub(total_out)}")
        msg = head + msg + ["Ещё части выписки — присылайте, всё — /done"]
        return [Reply("\n".join(msg) + warn)]

    def _done(self, chat_id, arg):
        state = self.db.get_state(chat_id)
        st = state.pop("statement", None)
        state.pop("sverka", None)
        if not st:
            self.db.set_state(chat_id, state)
            return [Reply("Сейчас не идёт загрузка выписки. /sverka — начать.")]
        months = [st["month"]] if st["month"] else sorted(st["months"])
        if not months:
            self.db.set_state(chat_id, state)
            return [Reply("Выписка не была загружена.")]

        replies, suggestions, unmatched = [], [], []
        for month in months:
            cs = next(c for c in summarize(self.db, month).cards if c.card.id == st["card_id"])
            replies.append(Reply(card_text(cs, month)))
            suggestions += [ln for ln in cs.unmatched_out if ln.suggested_category]
            unmatched += [ln for ln in cs.unmatched_out if not ln.suggested_category]

        state["review"] = [ln.id for ln in suggestions]
        self.db.set_state(chat_id, state)
        if suggestions:
            replies.append(Reply(suggestions_text(suggestions),
                                 [[("✅ Записать все как бизнес", "s:acc")]]))
        if unmatched and len(months) == 1:
            replies.append(Reply(unmatched_text(unmatched)))
        elif unmatched:
            replies.append(Reply(
                "Остальные списания считаются личными. Посмотреть их по месяцу "
                "и отметить бизнес: /vypiska 2026-03"))

        month = months[-1]
        rest = [c for c in summarize(self.db, month).cards if not c.has_statement]
        picker = f"s:m:{st['month'] or PERIOD}"
        itog = (f"s:itog:{month}" if len(months) == 1
                else f"s:itog:{months[0]}..{months[-1]}")
        next_btn = [[("Следующая карта", picker)], [("Итог", itog)]]
        if rest:
            replies.append(Reply("Без выписки пока: " + ", ".join(c.card.label for c in rest), next_btn))
        else:
            replies.append(Reply("Выписки по всем картам загружены.", next_btn[1:]))
        return replies

    def _vypiska(self, chat_id, arg):
        month = _parse_month_arg(arg) if arg else _month(self.today())
        if not month:
            return [Reply(MONTH_ARG_HELP)]
        lines = [ln for cs in summarize(self.db, month).cards for ln in cs.unmatched_out]
        if not lines:
            return [Reply(f"За {month_name(month)} нет списаний по выпискам, "
                          "которые считаются личными.")]
        return [Reply(f"{month_name(month).capitalize()}:\n" + unmatched_text(lines))]

    def _itog(self, chat_id, arg):
        today = self.today()
        if not arg:
            # В начале месяца обычно подводят итог прошлого.
            arg = _month(today, -1) if today.day <= 10 else _month(today)
        period = _parse_period_arg(arg, today)
        if not period:
            return [Reply("Укажите месяц (2026-09), год (2026) или период (2026-01..2026-09).")]
        first, last = period
        if first == last:
            summary = summarize(self.db, first)
            return [Reply(month_text(summary, self.db.owed_until(first)),
                          file=(f"свод-{first}.xlsx", month_xlsx(summary)))]
        months = _months_between(first, last)
        summaries = [summarize(self.db, m) for m in months]
        owed = self.db.owed_until(last)
        return [Reply(period_text(summaries, owed),
                      file=(f"свод-{first}--{last}.xlsx", period_xlsx(summaries, owed)))]

    def _biz(self, chat_id, arg):
        """Отметить строки выписки как бизнес-расходы.

        /biz 12 15 — выбранные строки; /biz все — все предложенные после /done.
        Строки с предложенной статьёй записываются без вопросов и одной сводкой.
        """
        state = self.db.get_state(chat_id)
        if arg.strip().lower() in ("все", "всё", "all"):
            ids = state.get("review", [])
            if not ids:
                return [Reply("Нет предложенных списаний. Сначала загрузите выписку: /sverka")]
        else:
            ids = [int(x) for x in arg.replace(",", " ").split() if x.isdigit()]
            if not ids:
                return [Reply("Укажите номера строк из сверки, например: /biz 12 15")]
        added = 0
        for line_id in ids:
            row = self.db.statement_line(line_id)
            if not row or row["direction"] != "out":
                continue
            category_id = self.db.category_id(row["suggested_category"])
            state.setdefault("drafts", []).append({
                "amount": row["amount"], "date": row["op_date"], "card_id": row["card_id"],
                "kind": EXPENSE, "purpose": BUSINESS, "purpose_asked": True,
                "category_id": category_id, "category_confident": category_id is not None,
                "merchant": row["description"], "description": "по выписке",
                "receipt": "", "note": "", "dup_checked": False, "quiet": True,
            })
            added += 1
        state["review"] = [i for i in state.get("review", []) if i not in ids]
        if not state["review"]:
            del state["review"]
        if not added:
            self.db.set_state(chat_id, state)
            return [Reply("Не нашёл таких списаний в выписках.")]
        self.db.set_state(chat_id, state)
        return self._advance(chat_id)

    def _notbiz(self, chat_id, arg):
        ids = [int(x) for x in arg.replace(",", " ").split() if x.isdigit()]
        if not ids:
            return [Reply("Укажите номера, которые не бизнес, например: /notbiz 12 15")]
        state = self.db.get_state(chat_id)
        for line_id in ids:
            self.db.clear_suggestion(line_id)
        left = [i for i in state.get("review", []) if i not in ids]
        state["review"] = left
        if not left:
            del state["review"]
        self.db.set_state(chat_id, state)
        if not left:
            return [Reply("Убрал. Предложений больше не осталось.")]
        return [Reply(f"Убрал из предложенных: {len(ids)}. Осталось {len(left)}.",
                      [[("✅ Записать оставшиеся как бизнес", "s:acc")]])]
