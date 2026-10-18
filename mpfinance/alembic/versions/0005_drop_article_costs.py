"""Удалить себестоимость, в которую попал артикул вместо цены.

18.10.2026 на бою справочник баркодов 1С (`uid|артикул|наименование|баркод|
размер|цвет`) загрузили на «Диагностике» как файл себестоимости. Разбор тогда
брал второе поле и убирал из него пробелы: у товаров с цифровым артикулом
записалась «себестоимость» $7 053 410 146 (артикул «70534 10146») и подобное, а
дашборд показал себестоимость WB 692 млрд ₽. Разбор теперь строгий
(`costs.parse_cost_file`), а здесь убирается то, что успело записаться.

Удаляется ТОЛЬКО запись из 1С, чья цена в точности равна артикулу её товара,
прочитанному тем же прежним правилом (без пробелов, запятая — точка, × 100
центов). Совпасть с настоящей себестоимостью это может лишь у короткого
цифрового артикула («010» — $10), и тогда цена просто придёт заново со
следующей выгрузкой себестоимости из 1С. Ручные цены (Excel) не трогаются
никогда. Повторный прогон ничего не находит.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18 17:10:00

"""
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def _as_old_parser_read(article: str) -> int | None:
    text = "".join((article or "").split()).replace(",", ".")
    try:
        price = Decimal(text)
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0:
        return None
    return int((price * 100).to_integral_value())


def upgrade():
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT c.id, c.cost_minor, p.article FROM product_costs c "
        "JOIN products p ON p.uid_1c = c.uid_1c WHERE c.source != 'excel'")).fetchall()
    doomed = [cid for cid, cost, article in rows if cost == _as_old_parser_read(article)]
    for start in range(0, len(doomed), 500):
        chunk = doomed[start:start + 500]
        bind.execute(sa.text("DELETE FROM product_costs WHERE id IN (%s)" % ",".join(str(int(i)) for i in chunk)))
    if doomed:
        bind.execute(sa.text(
            "INSERT INTO audit_log (at, actor, action, details) VALUES (CURRENT_TIMESTAMP, 'миграция', "
            "'product_costs_cleanup', :d)"),
            {"d": f"удалена себестоимость, в которую попал артикул вместо цены: {len(doomed)} записей"})


def downgrade():
    # Возвращать мусор нечем и незачем.
    pass
