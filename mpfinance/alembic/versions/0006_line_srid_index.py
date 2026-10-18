"""Индекс по номеру заказа в строках отчётов — защита от повторов
(`finance/repeats.py`) ищет продажу по нему при каждой загрузке.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18 18:20:00

"""
import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

NAME = 'ix_sales_report_lines_srid'


def _has_index() -> bool:
    return any(i["name"] == NAME for i in sa.inspect(op.get_bind()).get_indexes('sales_report_lines'))


def upgrade():
    # Оборванный прогон на SQLite alembic не откатывает: повтор не должен падать.
    if not _has_index():
        op.create_index(NAME, 'sales_report_lines', ['srid'], unique=False)


def downgrade():
    if _has_index():
        op.drop_index(NAME, table_name='sales_report_lines')
