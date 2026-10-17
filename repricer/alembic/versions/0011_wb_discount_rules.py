"""Порог скидки от эталона WB

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17

Таблица `wb_discount_rules` (порог по категории WB или артикулу 1С, на все кабинеты или на один) и колонка
`reference_prices.held_pct` (на WB стоит эталон − N%). Каждый шаг спрашивает, не сделан ли он
уже (alembic на SQLite упавшую миграцию не откатывает).
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'wb_discount_rules' not in set(sa.inspect(bind).get_table_names()):
        op.create_table('wb_discount_rules',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(length=8), nullable=False),
            sa.Column('key', sa.String(length=200), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('max_disc', sa.Numeric(5, 2), nullable=True),
            sa.Column('now_pct', sa.Numeric(5, 2), nullable=True),
            sa.Column('now_at', sa.DateTime(), nullable=True),
            sa.Column('now_by', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(length=64), nullable=False, server_default=''),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('kind', 'key', 'account_id', name='uq_wb_discount_rule'))
    cols = {c['name'] for c in sa.inspect(bind).get_columns('reference_prices')}
    if 'held_pct' not in cols:
        # ADD COLUMN на SQLite — без перестройки таблицы, мгновенно и при живой службе.
        op.add_column('reference_prices', sa.Column('held_pct', sa.Numeric(5, 2), nullable=True))


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
