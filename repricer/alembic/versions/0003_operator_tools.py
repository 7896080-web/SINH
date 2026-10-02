"""Инструменты оператора: статус и минимальная цена площадки, сохранённые отборы

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02

Каждый шаг спрашивает, не сделан ли он уже: alembic на SQLite упавшую
миграцию не откатывает.
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _add_column(table, column):
    if column.name not in {c["name"] for c in _insp().get_columns(table)}:
        op.add_column(table, column)


def upgrade():
    _add_column('platform_items', sa.Column('price_status', sa.String(length=16), nullable=True))
    _add_column('platform_items', sa.Column('min_price', sa.Integer(), nullable=True))
    if 'saved_filters' not in set(_insp().get_table_names()):
        op.create_table('saved_filters',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('url', sa.String(length=1000), nullable=False),
            sa.Column('created_by', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'))


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
