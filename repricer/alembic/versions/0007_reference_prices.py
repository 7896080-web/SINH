"""Эталонные цены WB

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09

Таблица `reference_prices`: последняя отправленная цена SKU в кабинете WB, к получению
по ней и курс, по которому она посчитана. Шаг спрашивает, не сделан ли он уже
(alembic на SQLite упавшую миграцию не откатывает).
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    if 'reference_prices' not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table('reference_prices',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('item_id', sa.String(length=64), nullable=False),
            sa.Column('ref_price', sa.Integer(), nullable=False),
            sa.Column('ref_payout', sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column('commission_percent', sa.Numeric(precision=6, scale=2), nullable=True),
            sa.Column('discount', sa.Numeric(precision=6, scale=4), nullable=False, server_default='0'),
            sa.Column('usd_rub', sa.Numeric(precision=12, scale=4), nullable=True),
            sa.Column('source', sa.String(length=16), nullable=False, server_default='sent'),
            sa.Column('set_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'item_id', name='uq_reference_price'))
        op.create_index('ix_reference_prices_item_id', 'reference_prices', ['item_id'])


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
