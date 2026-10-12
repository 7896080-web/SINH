"""Экономика карточек для страницы «Утверждение цен»

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09

Таблица `card_econ`: k, W/P, логистика и нетто-продажи по паре «карточка + артикул 1С» кабинета.
Шаг спрашивает, не сделан ли он уже (alembic на SQLite упавшую миграцию не откатывает).
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    if 'card_econ' not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table('card_econ',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('card', sa.String(length=64), nullable=False),
            sa.Column('article', sa.String(length=200), nullable=False),
            sa.Column('net_units', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('k', sa.Numeric(precision=8, scale=5), nullable=True),
            sa.Column('w', sa.Numeric(precision=8, scale=5), nullable=True),
            sa.Column('f_logistics', sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column('how', sa.String(length=200), nullable=False, server_default=''),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'card', 'article', name='uq_card_econ'))


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
