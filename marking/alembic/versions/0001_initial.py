"""Начальная схема программы маркировки

Revision ID: 0001
Revises: 
Create Date: 2026-09-29 01:07:13.236943

"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _create_table(name, *columns, **kw):
    """Миграция обязана переживать собственный обрыв и таблицы, уже созданные
    приложением (`create_all` при старте): на SQLite alembic упавшую миграцию
    не откатывает, и повторный `upgrade head` иначе падал бы навсегда."""
    if name not in _tables():
        op.create_table(name, *columns, **kw)


def _create_index(table, name, columns, unique=False):
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, unique=unique)



def upgrade():
    _create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('object', sa.String(length=100), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('audit_log', 'ix_audit_log_at', ['at'], unique=False)

    _create_table('catalog_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supplier_sku', sa.String(length=300), nullable=False),
    sa.Column('ean', sa.String(length=20), nullable=False),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('lamoda_sku', sa.String(length=64), nullable=False),
    sa.Column('parent_sku', sa.String(length=300), nullable=False),
    sa.Column('size', sa.String(length=50), nullable=False),
    sa.Column('color', sa.String(length=100), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('tn_ved', sa.String(length=20), nullable=False),
    sa.Column('tax_class', sa.String(length=20), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('supplier_sku')
    )
    _create_index('catalog_items', 'ix_catalog_items_ean', ['ean'], unique=False)

    _create_table('organizations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('surname', sa.String(length=100), nullable=False),
    sa.Column('firstname', sa.String(length=100), nullable=False),
    sa.Column('patronymic', sa.String(length=100), nullable=False),
    sa.Column('inn', sa.String(length=12), nullable=False),
    sa.Column('ogrnip', sa.String(length=15), nullable=False),
    sa.Column('address', sa.String(length=300), nullable=False),
    sa.Column('signer_role', sa.String(length=50), nullable=False),
    sa.Column('vat_rate', sa.Integer(), nullable=False),
    sa.Column('contract_number', sa.String(length=50), nullable=False),
    sa.Column('sticker_sender', sa.String(length=200), nullable=False),
    sa.Column('edo_sender_id', sa.String(length=100), nullable=False),
    sa.Column('chz_contour', sa.String(length=20), nullable=False),
    sa.Column('oms_id', sa.String(length=64), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=True),
    sa.Column('nk_api_key_enc', sa.Text(), nullable=True),
    sa.Column('cert_thumbprint', sa.String(length=64), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('inn')
    )
    _create_table('settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    _create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )
    _create_table('worker_heartbeats',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('last_run_at', sa.DateTime(), nullable=True),
    sa.Column('last_success', sa.Boolean(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )
    _create_table('supplies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=20), nullable=False),
    sa.Column('doc_number', sa.String(length=20), nullable=False),
    sa.Column('supply_date', sa.Date(), nullable=True),
    sa.Column('planned_upd_date', sa.Date(), nullable=True),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('scheme_choice', sa.String(length=20), nullable=False),
    sa.Column('scheme_reason', sa.Text(), nullable=False),
    sa.Column('source_filename', sa.String(length=300), nullable=False),
    sa.Column('extra_headers', sa.JSON(), nullable=False),
    sa.Column('onec_document', sa.String(length=50), nullable=False),
    sa.Column('moved_at', sa.DateTime(), nullable=True),
    sa.Column('is_test', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('doc_number'),
    sa.UniqueConstraint('number')
    )
    _create_table('fbo_uploads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supply_id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=300), nullable=False),
    sa.Column('content', sa.LargeBinary(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('report', sa.Text(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('uploaded_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['supply_id'], ['supplies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('fbo_uploads', 'ix_fbo_uploads_supply_id', ['supply_id'], unique=False)

    _create_table('onec_tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('command', sa.String(length=32), nullable=False),
    sa.Column('order_id', sa.String(length=64), nullable=False),
    sa.Column('line', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('filename', sa.String(length=100), nullable=False),
    sa.Column('supply_id', sa.Integer(), nullable=True),
    sa.Column('result_status', sa.String(length=20), nullable=False),
    sa.Column('result_detail', sa.Text(), nullable=False),
    sa.Column('is_test', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('answered_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['supply_id'], ['supplies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('onec_tasks', 'ix_onec_tasks_order_id', ['order_id'], unique=False)
    _create_index('onec_tasks', 'ix_onec_tasks_status', ['status'], unique=False)
    _create_index('onec_tasks', 'ix_onec_tasks_supply_id', ['supply_id'], unique=False)

    _create_table('supply_rows',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supply_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('supplier_sku', sa.String(length=300), nullable=False),
    sa.Column('qty', sa.Integer(), nullable=False),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('ean', sa.String(length=20), nullable=False),
    sa.Column('file_price', sa.String(length=50), nullable=False),
    sa.Column('file_ean', sa.String(length=20), nullable=False),
    sa.Column('extras', sa.JSON(), nullable=False),
    sa.Column('warnings', sa.Text(), nullable=False),
    sa.Column('onec_status', sa.String(length=20), nullable=False),
    sa.Column('onec_item_id', sa.String(length=64), nullable=False),
    sa.Column('onec_article', sa.String(length=200), nullable=False),
    sa.Column('onec_name', sa.String(length=500), nullable=False),
    sa.Column('onec_size', sa.String(length=50), nullable=False),
    sa.Column('onec_color', sa.String(length=100), nullable=False),
    sa.Column('onec_stock', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['supply_id'], ['supplies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('supply_rows', 'ix_supply_rows_supply_id', ['supply_id'], unique=False)

    _create_table('upd_documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supply_id', sa.Integer(), nullable=False),
    sa.Column('fbo_upload_id', sa.Integer(), nullable=False),
    sa.Column('doc_number', sa.String(length=20), nullable=False),
    sa.Column('doc_date', sa.Date(), nullable=False),
    sa.Column('scheme', sa.String(length=20), nullable=False),
    sa.Column('scheme_manual', sa.Boolean(), nullable=False),
    sa.Column('scheme_reason', sa.Text(), nullable=False),
    sa.Column('id_file', sa.String(length=200), nullable=False),
    sa.Column('xml', sa.LargeBinary(), nullable=False),
    sa.Column('positions', sa.Integer(), nullable=False),
    sa.Column('total_with_vat', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('check_report', sa.Text(), nullable=False),
    sa.Column('has_errors', sa.Boolean(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['fbo_upload_id'], ['fbo_uploads.id'], ),
    sa.ForeignKeyConstraint(['supply_id'], ['supplies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('doc_number', name='uq_upd_doc_number')
    )
    _create_index('upd_documents', 'ix_upd_documents_supply_id', ['supply_id'], unique=False)





def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('upd_documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_upd_documents_supply_id'))

    op.drop_table('upd_documents')
    with op.batch_alter_table('supply_rows', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_supply_rows_supply_id'))

    op.drop_table('supply_rows')
    with op.batch_alter_table('onec_tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_onec_tasks_supply_id'))
        batch_op.drop_index(batch_op.f('ix_onec_tasks_status'))
        batch_op.drop_index(batch_op.f('ix_onec_tasks_order_id'))

    op.drop_table('onec_tasks')
    with op.batch_alter_table('fbo_uploads', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fbo_uploads_supply_id'))

    op.drop_table('fbo_uploads')
    op.drop_table('supplies')
    op.drop_table('worker_heartbeats')
    op.drop_table('users')
    op.drop_table('settings')
    op.drop_table('organizations')
    with op.batch_alter_table('catalog_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_catalog_items_ean'))

    op.drop_table('catalog_items')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_at'))

    op.drop_table('audit_log')
    # ### end Alembic commands ###
