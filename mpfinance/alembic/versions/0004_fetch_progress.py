"""Шкала прогресса загрузок по API: сделано шагов, всего шагов, что делается.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18 10:40:00

"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

COLUMNS = (
    sa.Column('progress_done', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('progress_total', sa.Integer(), nullable=True),
    sa.Column('progress_note', sa.String(length=200), nullable=True),
)


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("report_fetches")}


def upgrade():
    # На SQLite alembic оборванную миграцию не откатывает: повтор не должен
    # падать на «duplicate column name».
    have = _existing()
    for column in COLUMNS:
        if column.name not in have:
            op.add_column('report_fetches', column.copy())


def downgrade():
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_report_fetches")
    have = _existing()
    with op.batch_alter_table('report_fetches') as batch:
        for column in reversed(COLUMNS):
            if column.name in have:
                batch.drop_column(column.name)
