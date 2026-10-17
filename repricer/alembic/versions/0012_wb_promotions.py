"""Календарь акций WB и порог «для акции»

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-17

Таблицы `wb_promotions` (акции по кабинетам) и `wb_promotion_items` (товары обычных акций),
колонки `wb_discount_rules.until / prev_max_disc / until_promo_id / until_note` — порог, поставленный
до конца акции. Каждый шаг спрашивает, не сделан ли он уже (alembic на SQLite упавшую миграцию не
откатывает).
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'wb_promotions' not in tables:
        op.create_table('wb_promotions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('promo_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=300), nullable=False, server_default=''),
            sa.Column('type', sa.String(length=16), nullable=False, server_default='regular'),
            sa.Column('start_at', sa.DateTime(), nullable=True),
            sa.Column('end_at', sa.DateTime(), nullable=True),
            sa.Column('description', sa.Text(), nullable=False, server_default=''),
            sa.Column('advantages', sa.Text(), nullable=False, server_default=''),
            sa.Column('participation', sa.Integer(), nullable=True),
            sa.Column('in_action_total', sa.Integer(), nullable=True),
            sa.Column('exceptions', sa.Integer(), nullable=True),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            sa.Column('items_fetched_at', sa.DateTime(), nullable=True),
            sa.Column('items_note', sa.Text(), nullable=False, server_default=''),
            sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'promo_id', name='uq_wb_promotion'))
    if 'wb_promotion_items' not in tables:
        op.create_table('wb_promotion_items',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('promo_id', sa.Integer(), nullable=False),
            sa.Column('nm_id', sa.String(length=32), nullable=False),
            sa.Column('in_action', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('price', sa.Integer(), nullable=True),
            sa.Column('plan_price', sa.Integer(), nullable=True),
            sa.Column('discount', sa.Integer(), nullable=True),
            sa.Column('plan_discount', sa.Integer(), nullable=True),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            sa.Column('added_at', sa.DateTime(), nullable=True),
            sa.Column('added_by', sa.String(length=64), nullable=False, server_default=''),
            sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'promo_id', 'nm_id', name='uq_wb_promotion_item'))
    have = {i['name'] for i in sa.inspect(bind).get_indexes('wb_promotion_items')}
    if 'ix_wb_promotion_items_promo_id' not in have:
        op.create_index('ix_wb_promotion_items_promo_id', 'wb_promotion_items', ['promo_id'], unique=False)
    cols = {c['name'] for c in sa.inspect(bind).get_columns('wb_discount_rules')}
    # ADD COLUMN на SQLite — без перестройки таблицы, мгновенно и при живой службе.
    if 'until' not in cols:
        op.add_column('wb_discount_rules', sa.Column('until', sa.DateTime(), nullable=True))
    if 'prev_max_disc' not in cols:
        op.add_column('wb_discount_rules', sa.Column('prev_max_disc', sa.Numeric(5, 2), nullable=True))
    if 'until_promo_id' not in cols:
        op.add_column('wb_discount_rules', sa.Column('until_promo_id', sa.Integer(), nullable=True))
    if 'until_note' not in cols:
        op.add_column('wb_discount_rules', sa.Column('until_note', sa.String(length=300), nullable=False,
                                                     server_default=''))


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
