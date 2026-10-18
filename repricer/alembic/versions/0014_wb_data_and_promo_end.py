"""Данные WB для оценки цен и акций; возврат эталона после конца акции

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-18

Таблицы сбора данных WB (`wb_data.py`): `wb_data_state` (где остановился сбор), `wb_orders`, `wb_sales`,
`wb_funnel_days`, `wb_stock_days`, `wb_price_days`, `wb_finance_rows`. Колонки `wb_promotion_items.end_done_at /
end_note` — акция закончилась, карточке вернули эталон (`wb_promo.end_due`). Каждый шаг спрашивает, не сделан ли
он уже (alembic на SQLite упавшую миграцию не откатывает); ADD COLUMN — без перестройки таблицы.
"""
from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def _fk():
    return sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE')


def _index(bind, table, cols):
    have = {i['name'] for i in sa.inspect(bind).get_indexes(table)}
    for col in cols:
        name = f'ix_{table}_{col}'
        if name not in have:
            op.create_index(name, table, [col], unique=False)


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    cols = {c['name'] for c in sa.inspect(bind).get_columns('wb_promotion_items')}
    if 'end_done_at' not in cols:
        op.add_column('wb_promotion_items', sa.Column('end_done_at', sa.DateTime(), nullable=True))
    if 'end_note' not in cols:
        op.add_column('wb_promotion_items', sa.Column('end_note', sa.String(length=200), nullable=False,
                                                      server_default=''))
    if 'wb_data_state' not in tables:
        op.create_table('wb_data_state',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('dataset', sa.String(length=16), nullable=False),
            sa.Column('key', sa.String(length=16), nullable=False, server_default=''),
            sa.Column('cursor', sa.Text(), nullable=False, server_default=''),
            sa.Column('done_at', sa.DateTime(), nullable=True),
            sa.Column('next_at', sa.DateTime(), nullable=True),
            sa.Column('rows', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('error', sa.Text(), nullable=False, server_default=''),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            _fk(), sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'dataset', 'key', name='uq_wb_data_state'))
    common = lambda: [  # noqa: E731
        sa.Column('nm_id', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('barcode', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('article', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('size', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('total_price', sa.Numeric(12, 2), nullable=True),
        sa.Column('discount_pct', sa.Integer(), nullable=True),
        sa.Column('spp', sa.Numeric(6, 2), nullable=True),
        sa.Column('price_with_disc', sa.Numeric(12, 2), nullable=True),
        sa.Column('finished_price', sa.Numeric(12, 2), nullable=True)]
    if 'wb_orders' not in tables:
        op.create_table('wb_orders',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('srid', sa.String(length=100), nullable=False),
            sa.Column('day', sa.Date(), nullable=False),
            sa.Column('ordered_at', sa.DateTime(), nullable=True),
            sa.Column('changed_at', sa.DateTime(), nullable=True),
            *common(),
            sa.Column('is_cancel', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('cancel_at', sa.DateTime(), nullable=True),
            sa.Column('warehouse', sa.String(length=100), nullable=False, server_default=''),
            sa.Column('region', sa.String(length=200), nullable=False, server_default=''),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            _fk(), sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'srid', name='uq_wb_order'))
    _index(bind, 'wb_orders', ['day', 'nm_id'])
    if 'wb_sales' not in tables:
        op.create_table('wb_sales',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('sale_id', sa.String(length=64), nullable=False),
            sa.Column('srid', sa.String(length=100), nullable=False, server_default=''),
            sa.Column('day', sa.Date(), nullable=False),
            sa.Column('sold_at', sa.DateTime(), nullable=True),
            sa.Column('changed_at', sa.DateTime(), nullable=True),
            *common(),
            sa.Column('for_pay', sa.Numeric(12, 2), nullable=True),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            _fk(), sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'sale_id', name='uq_wb_sale'))
    _index(bind, 'wb_sales', ['day', 'nm_id'])
    if 'wb_funnel_days' not in tables:
        op.create_table('wb_funnel_days',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('day', sa.Date(), nullable=False),
            sa.Column('nm_id', sa.String(length=32), nullable=False),
            *[sa.Column(c, sa.Integer(), nullable=False, server_default='0') for c in (
                'open_count', 'cart_count', 'order_count', 'order_sum', 'buyout_count', 'buyout_sum',
                'cancel_count', 'cancel_sum')],
            sa.Column('avg_price', sa.Integer(), nullable=True),
            sa.Column('wishlist', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            _fk(), sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'day', 'nm_id', name='uq_wb_funnel_day'))
    _index(bind, 'wb_funnel_days', ['day', 'nm_id'])
    if 'wb_stock_days' not in tables:
        op.create_table('wb_stock_days',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('day', sa.Date(), nullable=False),
            sa.Column('nm_id', sa.String(length=32), nullable=False),
            sa.Column('chrt_id', sa.String(length=32), nullable=False, server_default=''),
            sa.Column('quantity', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('to_client', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('from_client', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            _fk(), sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'day', 'nm_id', 'chrt_id', name='uq_wb_stock_day'))
    _index(bind, 'wb_stock_days', ['day', 'nm_id'])
    if 'wb_price_days' not in tables:
        op.create_table('wb_price_days',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('day', sa.Date(), nullable=False),
            sa.Column('nm_id', sa.String(length=32), nullable=False),
            sa.Column('price', sa.Integer(), nullable=True),
            sa.Column('sale_price', sa.Integer(), nullable=True),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            _fk(), sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'day', 'nm_id', name='uq_wb_price_day'))
    _index(bind, 'wb_price_days', ['day', 'nm_id'])
    if 'wb_finance_rows' not in tables:
        money = lambda name: sa.Column(name, sa.Numeric(12, 2), nullable=True)  # noqa: E731
        pct = lambda name: sa.Column(name, sa.Numeric(8, 4), nullable=True)     # noqa: E731
        op.create_table('wb_finance_rows',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('rrd_id', sa.String(length=32), nullable=False),
            sa.Column('report_id', sa.String(length=32), nullable=False, server_default=''),
            sa.Column('period_from', sa.Date(), nullable=True),
            sa.Column('period_to', sa.Date(), nullable=True),
            sa.Column('day', sa.Date(), nullable=True),
            sa.Column('nm_id', sa.String(length=32), nullable=False, server_default=''),
            sa.Column('barcode', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('article', sa.String(length=200), nullable=False, server_default=''),
            sa.Column('size', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('doc_type', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('oper', sa.String(length=100), nullable=False, server_default=''),
            sa.Column('srid', sa.String(length=100), nullable=False, server_default=''),
            sa.Column('ordered_at', sa.DateTime(), nullable=True),
            sa.Column('sold_at', sa.DateTime(), nullable=True),
            sa.Column('quantity', sa.Integer(), nullable=True),
            money('retail_price'), money('retail_amount'), money('retail_price_with_disc'),
            sa.Column('sale_pct', sa.Integer(), nullable=True),
            pct('commission_pct'), pct('kvw_base'), pct('kvw'), pct('kgvp_v2'),
            sa.Column('spp', sa.Numeric(6, 2), nullable=True),
            money('for_pay'), money('sales_commission'), money('acquiring_fee'), pct('acquiring_pct'),
            sa.Column('delivery_count', sa.Integer(), nullable=True),
            sa.Column('return_count', sa.Integer(), nullable=True),
            money('logistics'), money('penalty'), money('additional_payment'), money('storage'),
            money('deduction'), money('acceptance'), money('rebill_logistics'),
            sa.Column('seller_promo_id', sa.String(length=32), nullable=False, server_default=''),
            pct('seller_promo_discount'),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            _fk(), sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('account_id', 'rrd_id', name='uq_wb_finance_row'))
    _index(bind, 'wb_finance_rows', ['day', 'nm_id'])


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
