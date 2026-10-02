"""Базовая цена товара и цена товара на площадке (для всех её кабинетов)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02

Каждый шаг спрашивает, не сделан ли он уже: alembic на SQLite упавшую
миграцию не откатывает.
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if 'base_prices' not in tables:
        op.create_table('base_prices',
            sa.Column('item_id', sa.String(length=64), nullable=False),
            sa.Column('price', sa.Integer(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(length=64), nullable=False),
            sa.PrimaryKeyConstraint('item_id'))
    if 'platform_prices' not in tables:
        op.create_table('platform_prices',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('item_id', sa.String(length=64), nullable=False),
            sa.Column('platform', sa.String(length=8), nullable=False),
            sa.Column('price', sa.Integer(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(length=64), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('item_id', 'platform', name='uq_platform_price'))
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes('platform_prices')}
    if 'ix_platform_prices_item_id' not in existing:
        op.create_index('ix_platform_prices_item_id', 'platform_prices', ['item_id'])


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
