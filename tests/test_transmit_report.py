"""Отчёт «что уходит сейчас» считает ТОЙ ЖЕ лестницей, что и рассылка.

Смысл отчёта — ответить на вопрос «сколько уедет при следующей отправке», и
ответ обязан совпадать с тем, что реально посчитает `dispatch`. Повтори он
лестницу у себя — и однажды покажет не то, что произойдёт: именно так уже
расходились интерфейс и рассылка до того, как расчёт свели в один модуль
(в `app/transmit.py` про это сказано первым же абзацем).

Тест сканирующий, потому что предмет правки — ВЫЗОВ, а не число: подставить
формулу «остаток − порог» прямо в отчёт легко и выглядит безобидно.
"""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "transmit_report.py"


def test_the_report_calls_the_same_ladder_the_dispatcher_does():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "from app.transmit import explain" in source
    assert "explain(product, setting, account)" in source


def test_the_report_does_not_recompute_the_ladder_itself():
    """Ни одной собственной формулы остатка: только то, что вернул `explain`."""
    source = SCRIPT.read_text(encoding="utf-8")
    code = [line for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")]

    for line in code:
        assert "stock_on_hand -" not in line, f"лестница пересчитана в отчёте: {line}"
        assert "broadcast_offset)" not in line or "explain" in line or \
               "isnot" in line or "is not None" in line, \
               f"порог применяется мимо `explain`: {line}"


def test_a_truncated_report_says_so():
    """Молча обрезанный отчёт хуже отсутствующего: по нему судят обо всём
    каталоге. Та же обязанность, что у выгрузок и у клиентов площадок."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "ВНИМАНИЕ: под отбор попало" in source
    assert "total > len(products)" in source


def test_the_totals_are_not_compared_as_one_number():
    """Разность «уйдёт» минус «ушло» по всем парам не значит НИЧЕГО.

    В «ушло» не входят пары, куда не отправляли ни разу, зато входят пары с
    выключенной трансляцией — там «уйдёт» ноль, а «ушло» хранит прошлое число.
    Одна разность смешивает три обстоятельства и ни об одном не говорит правды,
    а читают её как «столько лишнего в продаже». Направления считаются порознь.
    """
    source = SCRIPT.read_text(encoding="utf-8")

    assert "never_sent" in source and "over_units" in source and "under_units" in source
    assert "площадка держит БОЛЬШЕ, чем уйдёт" in source, \
        "оверселл больше не назван отдельно"
    assert "не отправляли ни разу" in source, \
        "пары без отправок молча смешаны с остальными"


def test_it_never_writes_anything():
    """Скрипт для разбора инцидента — он обязан быть безопасным в любой момент."""
    source = SCRIPT.read_text(encoding="utf-8")

    for forbidden in ("db.commit()", "db.add(", "db.delete(", "db.flush()"):
        assert forbidden not in source, f"отчёт пишет в базу: {forbidden}"
