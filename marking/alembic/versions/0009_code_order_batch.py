"""Несколько GTIN в одном заказе СУЗ: строки заказа делят ключ

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16 18:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def _has_column(table, column):
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _has_index(table, name):
    return name in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade():
    # Переживает собственный обрыв и колонку, созданную приложением (create_all).
    if not _has_column('code_orders', 'batch_key'):
        op.add_column('code_orders', sa.Column('batch_key', sa.String(length=36), nullable=True))
    if not _has_index('code_orders', 'ix_code_orders_batch_key'):
        op.create_index('ix_code_orders_batch_key', 'code_orders', ['batch_key'])


def downgrade():
    op.drop_index('ix_code_orders_batch_key', table_name='code_orders')
    with op.batch_alter_table('code_orders') as batch:
        batch.drop_column('batch_key')
