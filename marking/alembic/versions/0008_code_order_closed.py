"""Заказ кодов помнит, что закрыт в СУЗ

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16 12:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def _has_column(table, column):
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Переживает собственный обрыв и колонку, созданную приложением (create_all).
    if not _has_column('code_orders', 'suz_closed_at'):
        op.add_column('code_orders', sa.Column('suz_closed_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('code_orders') as batch:
        batch.drop_column('suz_closed_at')
