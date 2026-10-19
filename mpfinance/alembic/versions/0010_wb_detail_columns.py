"""Детализация WB: цена продавца, «реализовано WB», кВВ, эквайринг, вид начисления, склад.

Экономика WB считается от цены продавца («Цена розничная с учётом согласованной
скидки»), налог — от «Вайлдберриз реализовал» (решение владельца 19.10.2026).
Для уже загруженных строк «реализовано» равно выручке — так и считалось до сих
пор, налог по ним не меняется. Остальные колонки пусты, пока отчёт не дополнят
файлом детализации из кабинета WB.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19 03:10:00

"""
import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

LINE_COLUMNS = (
    ('realized_kop', sa.BigInteger()),
    ('list_price_kop', sa.BigInteger()),
    ('acquiring_kop', sa.BigInteger()),
    ('fee_pct_bp', sa.Integer()),
    ('charge_kind', sa.String(length=128)),
    ('warehouse', sa.String(length=128)),
)


def _columns(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Оборванный прогон на SQLite alembic не откатывает: каждый шаг спрашивает,
    # не сделан ли он уже.
    have = _columns('sales_report_lines')
    for name, type_ in LINE_COLUMNS:
        if name not in have:
            op.add_column('sales_report_lines', sa.Column(name, type_, nullable=True))
    if 'parser_version' not in _columns('sales_reports'):
        op.add_column('sales_reports', sa.Column('parser_version', sa.Integer(), nullable=False,
                                                 server_default='0'))
    op.get_bind().execute(sa.text(
        "UPDATE sales_report_lines SET realized_kop = revenue_kop WHERE realized_kop IS NULL"))


def downgrade():
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_sales_report_lines")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_sales_reports")
    have = _columns('sales_report_lines')
    with op.batch_alter_table('sales_report_lines') as batch:
        for name, _type in LINE_COLUMNS:
            if name in have:
                batch.drop_column(name)
    if 'parser_version' in _columns('sales_reports'):
        with op.batch_alter_table('sales_reports') as batch:
            batch.drop_column('parser_version')
