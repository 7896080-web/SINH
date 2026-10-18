"""Начальная схема программы «Финансы площадок».

Revision ID: 0001
Revises: 
Create Date: 2026-10-18 09:05:22.627006

"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=True),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('details', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_log_at'), ['at'], unique=False)

    op.create_table('barcodes',
    sa.Column('barcode', sa.String(length=64), nullable=False),
    sa.Column('uid_1c', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('barcode', 'uid_1c')
    )
    with op.batch_alter_table('barcodes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_barcodes_uid_1c'), ['uid_1c'], unique=False)

    op.create_table('currency_rates',
    sa.Column('rate_date', sa.Date(), nullable=False),
    sa.Column('usd_rub_e4', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('fetched_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('rate_date')
    )
    op.create_table('onec_tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('command', sa.String(length=32), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('line', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('requested_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('filename', sa.String(length=128), nullable=True),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('answered_at', sa.DateTime(), nullable=True),
    sa.Column('result_status', sa.String(length=16), nullable=True),
    sa.Column('result_detail', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('onec_tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_onec_tasks_command'), ['command'], unique=False)
        batch_op.create_index(batch_op.f('ix_onec_tasks_key'), ['key'], unique=False)
        batch_op.create_index(batch_op.f('ix_onec_tasks_status'), ['status'], unique=False)

    op.create_table('platform_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('sync_account_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('products',
    sa.Column('uid_1c', sa.String(length=64), nullable=False),
    sa.Column('article', sa.String(length=200), nullable=True),
    sa.Column('name', sa.String(length=500), nullable=True),
    sa.Column('size', sa.String(length=100), nullable=True),
    sa.Column('color', sa.String(length=100), nullable=True),
    sa.Column('seen_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('uid_1c')
    )
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_products_article'), ['article'], unique=False)

    op.create_table('settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )
    op.create_table('worker_heartbeats',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('last_run_at', sa.DateTime(), nullable=True),
    sa.Column('last_success', sa.Boolean(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('api_credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('field_name', sa.String(length=64), nullable=False),
    sa.Column('encrypted_value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'field_name', name='uq_credential_field')
    )
    with op.batch_alter_table('api_credentials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_credentials_account_id'), ['account_id'], unique=False)

    op.create_table('kit_variants',
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('variant_id', sa.String(length=64), nullable=False),
    sa.Column('barcode', sa.String(length=64), nullable=True),
    sa.Column('fetched_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
    sa.PrimaryKeyConstraint('account_id', 'variant_id')
    )
    op.create_table('product_costs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uid_1c', sa.String(length=64), nullable=False),
    sa.Column('valid_from', sa.Date(), nullable=False),
    sa.Column('cost_minor', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['uid_1c'], ['products.uid_1c'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uid_1c', 'valid_from', name='uq_cost_uid_date')
    )
    with op.batch_alter_table('product_costs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_product_costs_uid_1c'), ['uid_1c'], unique=False)

    op.create_table('report_fetches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('date_from', sa.Date(), nullable=True),
    sa.Column('date_to', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('requested_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('progress_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('found', sa.Integer(), nullable=False),
    sa.Column('imported', sa.Integer(), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('report_fetches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_report_fetches_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_report_fetches_status'), ['status'], unique=False)

    op.create_table('sales_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('report_number', sa.String(length=64), nullable=True),
    sa.Column('period_from', sa.Date(), nullable=False),
    sa.Column('period_to', sa.Date(), nullable=False),
    sa.Column('source_format', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('report_kind', sa.String(length=32), nullable=True),
    sa.Column('file_name', sa.String(length=255), nullable=True),
    sa.Column('file_sha256', sa.String(length=64), nullable=True),
    sa.Column('uploaded_by', sa.String(length=64), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(), nullable=True),
    sa.Column('lines_count', sa.Integer(), nullable=False),
    sa.Column('unmatched_lines', sa.Integer(), nullable=False),
    sa.Column('declared_payout_kop', sa.BigInteger(), nullable=True),
    sa.Column('bank_payout_kop', sa.BigInteger(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('onec_key', sa.String(length=64), nullable=False),
    sa.Column('onec_status', sa.Enum('none', 'pending', 'sent', 'done', 'failed', 'timeout', name='onecuploadstatus'), nullable=False),
    sa.Column('onec_requested_by', sa.String(length=64), nullable=True),
    sa.Column('onec_requested_at', sa.DateTime(), nullable=True),
    sa.Column('onec_sent_at', sa.DateTime(), nullable=True),
    sa.Column('onec_batch_filename', sa.String(length=128), nullable=True),
    sa.Column('onec_completed_at', sa.DateTime(), nullable=True),
    sa.Column('onec_detail', sa.String(length=255), nullable=True),
    sa.Column('onec_lines_sent', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('onec_key')
    )
    with op.batch_alter_table('sales_reports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_reports_file_sha256'), ['file_sha256'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_reports_platform'), ['platform'], unique=False)

    op.create_table('sales_report_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('row_no', sa.Integer(), nullable=True),
    sa.Column('operation', sa.String(length=16), nullable=False),
    sa.Column('source_operation', sa.String(length=128), nullable=True),
    sa.Column('operation_date', sa.Date(), nullable=False),
    sa.Column('order_date', sa.Date(), nullable=True),
    sa.Column('order_id', sa.String(length=128), nullable=True),
    sa.Column('srid', sa.String(length=128), nullable=True),
    sa.Column('barcode', sa.String(length=64), nullable=True),
    sa.Column('article', sa.String(length=128), nullable=True),
    sa.Column('platform_sku', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('size', sa.String(length=64), nullable=True),
    sa.Column('uid_1c', sa.String(length=64), nullable=True),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('revenue_kop', sa.BigInteger(), nullable=False),
    sa.Column('commission_kop', sa.BigInteger(), nullable=False),
    sa.Column('payout_kop', sa.BigInteger(), nullable=False),
    sa.Column('logistics_kop', sa.BigInteger(), nullable=False),
    sa.Column('storage_kop', sa.BigInteger(), nullable=False),
    sa.Column('penalty_kop', sa.BigInteger(), nullable=False),
    sa.Column('acceptance_kop', sa.BigInteger(), nullable=False),
    sa.Column('deduction_kop', sa.BigInteger(), nullable=False),
    sa.Column('compensation_kop', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['sales_reports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sales_report_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_report_lines_barcode'), ['barcode'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_report_lines_operation'), ['operation'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_report_lines_operation_date'), ['operation_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_report_lines_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_report_lines_report_id'), ['report_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_report_lines_uid_1c'), ['uid_1c'], unique=False)



def downgrade():
    with op.batch_alter_table('sales_report_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_report_lines_uid_1c'))
        batch_op.drop_index(batch_op.f('ix_sales_report_lines_report_id'))
        batch_op.drop_index(batch_op.f('ix_sales_report_lines_order_id'))
        batch_op.drop_index(batch_op.f('ix_sales_report_lines_operation_date'))
        batch_op.drop_index(batch_op.f('ix_sales_report_lines_operation'))
        batch_op.drop_index(batch_op.f('ix_sales_report_lines_barcode'))

    op.drop_table('sales_report_lines')
    with op.batch_alter_table('sales_reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_reports_platform'))
        batch_op.drop_index(batch_op.f('ix_sales_reports_file_sha256'))

    op.drop_table('sales_reports')
    with op.batch_alter_table('report_fetches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_report_fetches_status'))
        batch_op.drop_index(batch_op.f('ix_report_fetches_kind'))

    op.drop_table('report_fetches')
    with op.batch_alter_table('product_costs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_product_costs_uid_1c'))

    op.drop_table('product_costs')
    op.drop_table('kit_variants')
    with op.batch_alter_table('api_credentials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_credentials_account_id'))

    op.drop_table('api_credentials')
    op.drop_table('worker_heartbeats')
    op.drop_table('users')
    op.drop_table('settings')
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_products_article'))

    op.drop_table('products')
    op.drop_table('platform_accounts')
    with op.batch_alter_table('onec_tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_onec_tasks_status'))
        batch_op.drop_index(batch_op.f('ix_onec_tasks_key'))
        batch_op.drop_index(batch_op.f('ix_onec_tasks_command'))

    op.drop_table('onec_tasks')
    op.drop_table('currency_rates')
    with op.batch_alter_table('barcodes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_barcodes_uid_1c'))

    op.drop_table('barcodes')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_at'))

    op.drop_table('audit_log')
