"""Заказ кодов помнит свой OMS ID и сколько кодов СУЗ сообщил готовыми

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01 12:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

COLUMNS = (
    ('oms_id', sa.String(length=64)),
    ('available', sa.Integer()),
)


def _has_column(table, column):
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Переживает собственный обрыв и колонки, созданные приложением (create_all).
    for name, type_ in COLUMNS:
        if not _has_column('code_orders', name):
            op.add_column('code_orders', sa.Column(name, type_, nullable=True))


def downgrade():
    with op.batch_alter_table('code_orders') as batch:
        for name, _ in reversed(COLUMNS):
            batch.drop_column(name)
