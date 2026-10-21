"""Фаза 1: отличия от schema.sql, которых потребовал реальный API Lamoda v2 и страницы.

- order_items.lamoda_item_id BIGINT → TEXT: в OpenAPI v2 `items[].id` — строка;
  плюс SKU позиции (`sku`/`externalSku`) для позднего сопоставления.
- fbo_shipments: принятое количество (`quantityAccepted`) и склад.
- shipment_recommendations / purchase_recommendations: номер пересчёта, кто подтвердил,
  номер сборки поставки, снимок входов расчёта и пояснение, отметка «заказано».
- новые таблицы job_runs (журнал заданий + курсор опроса) и channel_sales_daily
  (дневные продажи не-Lamoda каналов по баркоду).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('channel_sales_daily',
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('barcode', sa.Text(), nullable=False),
    sa.Column('sale_date', sa.Date(), nullable=False),
    sa.Column('qty', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('revenue', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('source', sa.Text(), nullable=True),
    sa.Column('loaded_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('channel', 'barcode', 'sale_date', name='channel_sales_daily_pkey')
    )
    op.create_table('job_runs',
    sa.Column('run_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('job', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'running'"), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('details', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('cursor', sa.Text(), nullable=True),
    sa.Column('started_by', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('run_id', name='job_runs_pkey')
    )
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.create_index('idx_job_runs_job_started', ['job', 'started_at'], unique=False)

    with op.batch_alter_table('fbo_shipments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('qty_accepted', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('warehouse_code', sa.Text(), nullable=True))

    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lamoda_sku', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('external_sku', sa.Text(), nullable=True))
        batch_op.alter_column('lamoda_item_id',
               existing_type=sa.BIGINT(),
               type_=sa.Text(),
               existing_nullable=True)

    with op.batch_alter_table('purchase_recommendations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('batch_id', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('stock_1c_qty_snapshot', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('stock_fbo_qty_snapshot', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('in_transit_qty_snapshot', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('on_order_qty_snapshot', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('daily_demand', sa.Numeric(precision=10, scale=3), nullable=True))
        batch_op.add_column(sa.Column('reorder_point_qty', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('explanation', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('ordered_qty', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('ordered_by', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('ordered_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('shipment_recommendations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('batch_id', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('confirmed_by', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('confirmed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('assembly_id', sa.BigInteger(), nullable=True))



def downgrade():
    with op.batch_alter_table('shipment_recommendations', schema=None) as batch_op:
        batch_op.drop_column('assembly_id')
        batch_op.drop_column('confirmed_at')
        batch_op.drop_column('confirmed_by')
        batch_op.drop_column('batch_id')

    with op.batch_alter_table('purchase_recommendations', schema=None) as batch_op:
        batch_op.drop_column('ordered_at')
        batch_op.drop_column('ordered_by')
        batch_op.drop_column('ordered_qty')
        batch_op.drop_column('explanation')
        batch_op.drop_column('reorder_point_qty')
        batch_op.drop_column('daily_demand')
        batch_op.drop_column('on_order_qty_snapshot')
        batch_op.drop_column('in_transit_qty_snapshot')
        batch_op.drop_column('stock_fbo_qty_snapshot')
        batch_op.drop_column('stock_1c_qty_snapshot')
        batch_op.drop_column('batch_id')

    with op.batch_alter_table('order_items', schema=None) as batch_op:
        # Обратно в BIGINT — только если все id числовые (иначе PostgreSQL откажет, и это правильно).
        batch_op.alter_column('lamoda_item_id',
               existing_type=sa.Text(),
               type_=sa.BIGINT(),
               existing_nullable=True,
               postgresql_using='lamoda_item_id::bigint')
        batch_op.drop_column('external_sku')
        batch_op.drop_column('lamoda_sku')

    with op.batch_alter_table('fbo_shipments', schema=None) as batch_op:
        batch_op.drop_column('warehouse_code')
        batch_op.drop_column('qty_accepted')

    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.drop_index('idx_job_runs_job_started')

    op.drop_table('job_runs')
    op.drop_table('channel_sales_daily')
