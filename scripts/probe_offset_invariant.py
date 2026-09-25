"""Инвариант `порог = расхождение + бронь` — независимая проверка. Только чтение.

Запуск на боевом сервере:

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_offset_invariant.py

Зачем он есть при живой находке отчёта. **Молчание проверки значит «инвариант
цел» ТОЛЬКО если проверке было что смотреть.** Проверка, которая ничего не
видит — отбор разошёлся со схемой, условие сузилось правкой, база пуста, — молчит
ровно так же, как проверка, которой нечего сказать. Скрипт печатает СНАЧАЛА,
сколько строк вообще попадает под её отбор, и лишь потом расхождения: ноль
кандидатов при зелёном отчёте это не «всё хорошо», а «отчёт слеп».

Формулу скрипт выводит САМ, обычной арифметикой, а не зовёт `offset_from_base`,
и это сделано НАМЕРЕННО — вопреки правилу «у формулы один хозяин», которое в
этом коде держит почти всё. Правило про боевые пути: там два счёта обязаны
совпадать по построению. Здесь предмет проверки — САМА формула, и позови скрипт
ту же функцию, он согласился бы с находкой всегда, что бы в ней ни сломалось.
Согласие двух независимых счётов — знание; согласие счёта с самим собой — нет.

Поэтому скрипт печатает ОБА числа рядом: своё и то, что насчитала находка.
Расхождение между ними — само по себе находка, и важнее любой отдельной строки:
оно означает, что отчёт считает не то, что здесь написано.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import and_, or_                           # noqa: E402

from app.database import SessionLocal                      # noqa: E402
from app.models import Product                             # noqa: E402


def expected_offset(disc, reserve, base_stock, fact) -> int | None:
    """Каким порог ОБЯЗАН быть. Своя арифметика, не `offset_from_base`."""
    reserve = reserve or 0
    if disc is not None:
        return disc + reserve
    if base_stock is None:
        return None
    # Строки, настроенные до появления колонки расхождения: факта нет либо он
    # равен учёту, и формула сводится к одной броне.
    return base_stock - ((base_stock if fact is None else fact) - reserve)


def main() -> int:
    db = SessionLocal()
    try:
        total = db.query(Product).count()
        casting = db.query(Product).filter(
            Product.broadcast_enabled.is_(True)).count()
        with_offset = db.query(Product).filter(
            Product.broadcast_enabled.is_(True),
            Product.broadcast_offset.isnot(None)).count()

        rows = db.query(
            Product.uid_1c, Product.article, Product.size, Product.color,
            Product.stock_on_hand, Product.reserve, Product.stock_discrepancy,
            Product.broadcast_offset, Product.offset_base_date,
            Product.offset_base_stock, Product.fact_at_date,
        ).filter(
            Product.broadcast_enabled.is_(True),
            Product.broadcast_offset.isnot(None),
            or_(
                Product.stock_discrepancy.isnot(None),
                and_(Product.offset_base_date.isnot(None),
                     Product.offset_base_stock.isnot(None)),
            ),
        ).order_by(Product.article).all()

        print("=== СКОЛЬКО СТРОК ВИДИТ ПРОВЕРКА")
        print(f"  товаров всего .............. {total}")
        print(f"  транслируются .............. {casting}")
        print(f"  из них с порогом ........... {with_offset}")
        print(f"  кандидатов на проверку ..... {len(rows)}")
        if not rows:
            print("\n  ВНИМАНИЕ: кандидатов НОЛЬ — молчание находки ничего не")
            print("  означает. Проверять нечего, а выглядит это как «всё хорошо».")
        print()

        bad, extra = [], 0
        for (uid, article, size, color, stock, reserve, disc, offset,
             base_date, base_stock, fact) in rows:
            want = expected_offset(disc, reserve, base_stock, fact)
            if want is None or offset == want:
                continue
            stock = stock or 0
            now, should = max(0, stock - offset), max(0, stock - want)
            if now > should:
                extra += now - should
            bad.append((uid, article, size, color, stock, reserve or 0, disc,
                        offset, want, now, should))

        print("=== РАСХОЖДЕНИЯ ПОРОГА С ФОРМУЛОЙ (свой счёт)")
        if not bad:
            print("  нет ни одного.")
        for (uid, article, size, color, stock, reserve, disc, offset, want,
             now, should) in bad[:200]:
            mark = "ЛИШНЕЕ В ПРОДАЖЕ" if now > should else "недодаём"
            disc_text = "не измерено" if disc is None else str(disc)
            print(f"  {article or uid} {size or ''} {color or ''}")
            print(f"      порог {offset}, а расхождение {disc_text} + бронь "
                  f"{reserve} дают {want}")
            print(f"      остаток {stock}: уходит {now}, должно {should}"
                  f"   <= {mark}")
        if bad:
            print(f"\n  строк: {len(bad)}"
                  + (f", лишних штук в продаже: {extra}" if extra else ""))
        print()

        # Второй счёт — тот, что показывает отчёт. Числа обязаны совпасть.
        from app.report import _q_offset_disagrees

        theirs = len(_q_offset_disagrees(db))
        print("=== СВЕРКА С НАХОДКОЙ ОТЧЁТА")
        print(f"  свой счёт ....... {len(bad)}")
        print(f"  находка ......... {theirs}")
        if theirs != len(bad):
            print("\n  ЧИСЛА РАЗОШЛИСЬ. Это важнее любой отдельной строки:")
            print("  отчёт считает не то, что описано в этом скрипте, —")
            print("  значит одна из двух формул съехала, и какая, знает только")
            print("  тот, кто их сравнит.")
            return 1
        print("  сошлись.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
