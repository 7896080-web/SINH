"""Командная строка: python -m finance <команда>. Вся логика — в ledger.py."""

import argparse
import os
import sys

from .ledger import EXPENSE, INCOME, KIND_LABELS, FinanceError, Ledger, current_month
from .money import format_amount

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".finance_assistant.db")
STATE_MARKS = {"ok": "  ", "fast": "! ", "over": "!!"}


def _money(kopecks: int) -> str:
    return f"{format_amount(kopecks):>14} ₽"


def _bar(percent: float, width: int = 20) -> str:
    filled = min(width, int(percent * width / 100))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="finance", description="Помощник по ведению личных финансов")
    p.add_argument("--db", default=os.environ.get("FINANCE_DB", DEFAULT_DB),
                   help="файл базы (по умолчанию $FINANCE_DB или ~/.finance_assistant.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    acc = sub.add_parser("account", help="счета: карта, наличные, вклад…").add_subparsers(dest="action", required=True)
    a = acc.add_parser("add", help="добавить счёт")
    a.add_argument("name")
    a.add_argument("--balance", default="0", help="начальный остаток")
    acc.add_parser("list", help="счета и остатки")

    cat = sub.add_parser("category", help="категории доходов и расходов").add_subparsers(dest="action", required=True)
    c = cat.add_parser("add", help="добавить категорию")
    c.add_argument("name")
    c.add_argument("--type", choices=[INCOME, EXPENSE], default=EXPENSE)
    cat.add_parser("list", help="список категорий")

    for kind, help_text in ((INCOME, "записать доход"), (EXPENSE, "записать расход")):
        t = sub.add_parser(kind, help=help_text)
        t.add_argument("amount")
        t.add_argument("category")
        t.add_argument("-a", "--account", required=True)
        t.add_argument("-d", "--date", help="ГГГГ-ММ-ДД или ДД.ММ.ГГГГ, по умолчанию сегодня")
        t.add_argument("-n", "--note", default="")

    tr = sub.add_parser("transfer", help="перевод между своими счетами")
    tr.add_argument("amount")
    tr.add_argument("--from", dest="from_account", required=True)
    tr.add_argument("--to", dest="to_account", required=True)
    tr.add_argument("-d", "--date")
    tr.add_argument("-n", "--note", default="")

    ls = sub.add_parser("list", help="журнал операций")
    ls.add_argument("-m", "--month", help="ГГГГ-ММ")
    ls.add_argument("-a", "--account")
    ls.add_argument("-c", "--category")

    rm = sub.add_parser("delete", help="удалить операцию по номеру")
    rm.add_argument("id", type=int)

    rep = sub.add_parser("report", help="итоги месяца: доходы, расходы, бюджеты")
    rep.add_argument("-m", "--month", default=None)

    tn = sub.add_parser("trend", help="доходы и расходы по месяцам")
    tn.add_argument("--months", type=int, default=6)

    bud = sub.add_parser("budget", help="лимиты расходов по категориям").add_subparsers(dest="action", required=True)
    b = bud.add_parser("set", help="задать лимит на месяц")
    b.add_argument("category")
    b.add_argument("amount")
    b.add_argument("-m", "--month", default=None)
    bs = bud.add_parser("show", help="исполнение бюджета")
    bs.add_argument("-m", "--month", default=None)
    bc = bud.add_parser("copy", help="перенести лимиты с одного месяца на другой")
    bc.add_argument("from_month")
    bc.add_argument("to_month")

    rec = sub.add_parser("recurring", help="регулярные платежи и поступления").add_subparsers(dest="action", required=True)
    r = rec.add_parser("add", help="добавить (аренда, зарплата, подписка…)")
    r.add_argument("name")
    r.add_argument("amount")
    r.add_argument("category")
    r.add_argument("-a", "--account", required=True)
    r.add_argument("--day", type=int, required=True, help="день месяца (31 = последний день)")
    r.add_argument("--type", choices=[INCOME, EXPENSE], default=EXPENSE)
    rec.add_parser("list", help="список регулярных операций")
    ra = rec.add_parser("apply", help="провести наступившие в месяце (повтор безопасен)")
    ra.add_argument("-m", "--month", default=None)

    ex = sub.add_parser("export", help="выгрузить все операции в CSV")
    ex.add_argument("file")
    im = sub.add_parser("import", help="загрузить операции из CSV (всё или ничего)")
    im.add_argument("file")
    return p


def run(args, ledger: Ledger, out) -> None:
    def say(line=""):
        print(line, file=out)

    month = getattr(args, "month", None) or current_month()

    if args.cmd == "account" and args.action == "add":
        ledger.add_account(args.name, args.balance)
        say(f"Счёт «{args.name}» добавлен.")
    elif args.cmd == "account":
        balances = ledger.balances()
        if not balances:
            say("Счетов пока нет. Добавьте: finance account add Карта --balance 10000")
        for name, bal in balances.items():
            say(f"{name:<24}{_money(bal)}")
        if balances:
            say(f"{'Итого':<24}{_money(sum(balances.values()))}")

    elif args.cmd == "category" and args.action == "add":
        ledger.add_category(args.name, args.type)
        say(f"Категория «{args.name}» ({KIND_LABELS[args.type]}) добавлена.")
    elif args.cmd == "category":
        for row in ledger.categories():
            say(f"{KIND_LABELS[row['kind']]:<8} {row['name']}")

    elif args.cmd in (INCOME, EXPENSE):
        add = ledger.add_income if args.cmd == INCOME else ledger.add_expense
        tx_id = add(args.amount, args.account, args.category, args.date, args.note)
        say(f"Записано (№{tx_id}). Остаток «{args.account}»: {format_amount(ledger.balances()[args.account])} ₽")
    elif args.cmd == "transfer":
        tx_id = ledger.add_transfer(args.amount, args.from_account, args.to_account, args.date, args.note)
        say(f"Перевод записан (№{tx_id}).")

    elif args.cmd == "list":
        txs = ledger.transactions(args.month, args.account, args.category)
        if not txs:
            say("Операций нет.")
        for t in txs:
            sign = {INCOME: "+", EXPENSE: "-"}.get(t.kind, "↔")
            where = f"{t.account} → {t.to_account}" if t.to_account else f"{t.account} / {t.category}"
            note = f"  ({t.note})" if t.note else ""
            say(f"{t.id:>5}  {t.date}  {sign}{format_amount(t.amount):>13}  {where}{note}")
    elif args.cmd == "delete":
        ledger.delete_transaction(args.id)
        say(f"Операция №{args.id} удалена.")

    elif args.cmd == "report":
        _print_report(ledger, month, say)
    elif args.cmd == "trend":
        say(f"{'Месяц':<9}{'Доходы':>16}{'Расходы':>16}{'Итог':>16}")
        for row in ledger.trend(args.months):
            say(f"{row['month']:<9}{_money(row['total_income'])}{_money(row['total_expense'])}{_money(row['net'])}")

    elif args.cmd == "budget" and args.action == "set":
        ledger.set_budget(args.category, args.amount, month)
        say(f"Лимит на «{args.category}» в {month} задан.")
    elif args.cmd == "budget" and args.action == "copy":
        n = ledger.copy_budgets(args.from_month, args.to_month)
        say(f"Перенесено лимитов: {n}.")
    elif args.cmd == "budget":
        _print_budgets(ledger, month, say)

    elif args.cmd == "recurring" and args.action == "add":
        ledger.add_recurring(args.name, args.type, args.amount, args.account, args.category, args.day)
        say(f"Регулярная операция «{args.name}» добавлена (каждое {args.day}-е число).")
    elif args.cmd == "recurring" and args.action == "apply":
        applied = ledger.apply_recurring(month)
        say("Проведено: " + ", ".join(applied) if applied else "Нечего проводить.")
    elif args.cmd == "recurring":
        for r in ledger.recurring():
            say(f"{r['day']:>2}-го  {KIND_LABELS[r['kind']]:<7} {format_amount(r['amount']):>13}  "
                f"{r['name']}  ({r['account']} / {r['category']})")

    elif args.cmd == "export":
        say(f"Выгружено операций: {ledger.export_csv(args.file)}.")
    elif args.cmd == "import":
        say(f"Загружено операций: {ledger.import_csv(args.file)}.")


def _print_report(ledger: Ledger, month: str, say) -> None:
    s = ledger.month_summary(month)
    say(f"Итоги за {s['month']}")
    say()
    say("Доходы:")
    for name, total in s["income"].items():
        say(f"  {name:<24}{_money(total)}")
    say(f"  {'Всего':<24}{_money(s['total_income'])}")
    say()
    say("Расходы:")
    for name, total in s["expense"].items():
        share = total * 100 / s["total_expense"]
        say(f"  {name:<24}{_money(total)}  {share:5.1f}%")
    say(f"  {'Всего':<24}{_money(s['total_expense'])}")
    say()
    say(f"Разница: {format_amount(s['net'])} ₽")
    if s["savings_rate"] is not None:
        say(f"Отложено от дохода: {s['savings_rate']}%")
    budgets = ledger.budget_status(month)
    if budgets:
        say()
        _print_budgets(ledger, month, say, budgets)


def _print_budgets(ledger: Ledger, month: str, say, rows=None) -> None:
    rows = rows if rows is not None else ledger.budget_status(month)
    if not rows:
        say(f"На {month} лимиты не заданы. Задайте: finance budget set Продукты 20000")
        return
    say(f"Бюджет на {month}:")
    for r in rows:
        say(f"{STATE_MARKS[r['state']]} {r['category']:<22}{_bar(r['percent'])} {r['percent']:6.1f}%  "
            f"потрачено {format_amount(r['spent'])} из {format_amount(r['limit'])}, "
            f"осталось {format_amount(r['left'])}")
    if any(r["state"] == "over" for r in rows):
        say("!! — лимит превышен")
    if any(r["state"] == "fast" for r in rows):
        say("!  — тратится быстрее, чем идёт месяц")


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    ledger = Ledger(args.db)
    try:
        run(args, ledger, out)
    except (FinanceError, ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        ledger.close()
    return 0
