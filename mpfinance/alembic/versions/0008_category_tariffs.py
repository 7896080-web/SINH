"""Тарифы комиссии по категориям (WB, по API) и предмет карточки WB.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-18 23:40:00

"""
import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

INDEX = 'ix_category_tariffs_category_name'


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    # Оборванный прогон на SQLite alembic не откатывает: повтор не должен падать.
    if 'category_tariffs' not in _tables():
        op.create_table(
            'category_tariffs',
            sa.Column('platform', sa.String(length=16), nullable=False),
            sa.Column('category_id', sa.String(length=64), nullable=False),
            sa.Column('category_name', sa.String(length=200), nullable=False),
            sa.Column('parent_name', sa.String(length=200), nullable=True),
            sa.Column('fbs_bp', sa.Integer(), nullable=True),
            sa.Column('fbo_bp', sa.Integer(), nullable=True),
            sa.Column('prev_fbs_bp', sa.Integer(), nullable=True),
            sa.Column('changed_at', sa.DateTime(), nullable=True),
            sa.Column('loaded_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('platform', 'category_id'),
        )
    if not any(i["name"] == INDEX for i in sa.inspect(op.get_bind()).get_indexes('category_tariffs')):
        op.create_index(INDEX, 'category_tariffs', ['category_name'], unique=False)
    if 'wb_cards' not in _tables():
        op.create_table(
            'wb_cards',
            sa.Column('nm_id', sa.String(length=32), nullable=False),
            sa.Column('subject_name', sa.String(length=200), nullable=False),
            sa.Column('seen_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('nm_id'),
        )


def downgrade():
    tables = _tables()
    if 'wb_cards' in tables:
        op.drop_table('wb_cards')
    if 'category_tariffs' in tables:
        op.drop_index(INDEX, table_name='category_tariffs')
        op.drop_table('category_tariffs')
