"""Категории и тарифы площадок, маржинальность на категорию, диапазон безопасности

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02

Каждый шаг спрашивает, не сделан ли он уже: alembic на SQLite упавшую
миграцию не откатывает. Колонки добавляются ALTER TABLE ADD COLUMN — без
перестройки таблиц; уборка `_alembic_tmp_` не нужна.
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def _cols(table):
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table, column):
    if column.name not in _cols(table):
        op.add_column(table, column)


def upgrade():
    _add('accounts', sa.Column('guard_min_margin', sa.Numeric(precision=7, scale=3), nullable=True))
    _add('accounts', sa.Column('guard_max_margin', sa.Numeric(precision=7, scale=3), nullable=True))
    _add('platform_items', sa.Column('category', sa.String(length=200), nullable=False, server_default=''))
    _add('platform_items', sa.Column('category_id', sa.String(length=64), nullable=False, server_default=''))
    _add('platform_items', sa.Column('tariff_fbs', sa.Numeric(precision=6, scale=2), nullable=True))
    _add('platform_items', sa.Column('tariff_fbo', sa.Numeric(precision=6, scale=2), nullable=True))
    _add('platform_items', sa.Column('tariff_loaded_at', sa.DateTime(), nullable=True))
    _add('platform_rules', sa.Column('commission_extra', sa.Numeric(precision=6, scale=2), nullable=False,
                                     server_default='0'))
    _add('platform_rules', sa.Column('tariff_model', sa.String(length=4), nullable=False, server_default='fbs'))
    _add('article_coefs', sa.Column('kind', sa.String(length=8), nullable=False, server_default='coef'))
    if 'category_targets' not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table('category_targets',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('platform', sa.String(length=8), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('category', sa.String(length=200), nullable=False),
            sa.Column('kind', sa.String(length=8), nullable=False),
            sa.Column('value', sa.Numeric(precision=7, scale=3), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(length=64), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('platform', 'account_id', 'category', name='uq_category_target'))


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
