"""Справочник баркодов 1С для страницы сопоставления

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01 18:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _create_index(table, name, columns, unique=False):
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, unique=unique)


def upgrade():
    # Переживает собственный обрыв и таблицу, созданную приложением (create_all).
    if 'onec_barcodes' not in _tables():
        op.create_table('onec_barcodes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('barcode', sa.String(length=64), nullable=False),
        sa.Column('item_id', sa.String(length=64), nullable=False),
        sa.Column('article', sa.String(length=200), nullable=False),
        sa.Column('name', sa.String(length=500), nullable=False),
        sa.Column('size', sa.String(length=100), nullable=False),
        sa.Column('color', sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('barcode', 'item_id', name='uq_onec_barcode_item')
        )
    _create_index('onec_barcodes', 'ix_onec_barcodes_barcode', ['barcode'])
    _create_index('onec_barcodes', 'ix_onec_barcodes_item_id', ['item_id'])


def downgrade():
    op.drop_table('onec_barcodes')
