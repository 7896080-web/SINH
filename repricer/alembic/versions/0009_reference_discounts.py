"""Эталонная скидка карточек WB

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-13

Таблица `reference_discounts`: скидка продавца по карточке (nmID), которую держит
`discount_guard.py`. Шаг спрашивает, не сделан ли он уже (alembic на SQLite упавшую
миграцию не откатывает).
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    if 'reference_discounts' not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table('reference_discounts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('nm_id', sa.String(length=32), nullable=False),
            sa.Column('discount', sa.Integer(), nullable=False),
            sa.Column('source', sa.String(length=16), nullable=False),
            sa.Column('set_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'nm_id', name='uq_reference_discount'))


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
