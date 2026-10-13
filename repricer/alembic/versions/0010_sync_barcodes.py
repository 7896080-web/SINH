"""Привязки баркодов из Sync Admin

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-13

Таблица `sync_barcodes`: баркод -> SKU 1С из выгрузки «Мэппинг» Sync Admin. Шаг спрашивает,
не сделан ли он уже (alembic на SQLite упавшую миграцию не откатывает).
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'sync_barcodes' not in set(sa.inspect(bind).get_table_names()):
        op.create_table('sync_barcodes',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('barcode', sa.String(length=64), nullable=False),
            sa.Column('item_id', sa.String(length=64), nullable=False),
            sa.Column('article', sa.String(length=200), nullable=False),
            sa.Column('name', sa.String(length=500), nullable=False),
            sa.Column('size', sa.String(length=100), nullable=False),
            sa.Column('color', sa.String(length=100), nullable=False),
            sa.Column('source', sa.String(length=32), nullable=False),
            sa.Column('loaded_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'))
    have = {i['name'] for i in sa.inspect(bind).get_indexes('sync_barcodes')}
    if 'ix_sync_barcodes_barcode' not in have:
        op.create_index('ix_sync_barcodes_barcode', 'sync_barcodes', ['barcode'], unique=True)
    if 'ix_sync_barcodes_item_id' not in have:
        op.create_index('ix_sync_barcodes_item_id', 'sync_barcodes', ['item_id'], unique=False)


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
