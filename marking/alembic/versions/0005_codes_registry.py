"""Реестр кодов: заказы СУЗ, коды поставки, документы ввода в оборот

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30 20:10:39.372091

"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _create_table(name, *columns, **kw):
    """Переживает собственный обрыв и таблицы, созданные приложением (create_all)."""
    if name not in _tables():
        op.create_table(name, *columns, **kw)


def _create_index(table, name, columns, unique=False):
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, unique=unique)


def _has_column(table, column):
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}



def upgrade():
    _create_table('code_orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supply_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('supplier_sku', sa.String(length=300), nullable=False),
    sa.Column('gtin', sa.String(length=14), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('suz_order_id', sa.String(length=64), nullable=False),
    sa.Column('received', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.ForeignKeyConstraint(['supply_id'], ['supplies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('code_orders', 'ix_code_orders_status', ['status'])
    _create_index('code_orders', 'ix_code_orders_supply_id', ['supply_id'])

    _create_table('introduce_docs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supply_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('document', sa.Text(), nullable=False),
    sa.Column('codes_count', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('doc_id', sa.String(length=64), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('checked_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.ForeignKeyConstraint(['supply_id'], ['supplies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('introduce_docs', 'ix_introduce_docs_status', ['status'])
    _create_index('introduce_docs', 'ix_introduce_docs_supply_id', ['supply_id'])

    _create_table('mark_codes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cis', sa.String(length=40), nullable=False),
    sa.Column('full_enc', sa.Text(), nullable=False),
    sa.Column('gtin', sa.String(length=14), nullable=False),
    sa.Column('supplier_sku', sa.String(length=300), nullable=False),
    sa.Column('supply_id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('status_at', sa.DateTime(), nullable=True),
    sa.Column('applied_at', sa.DateTime(), nullable=True),
    sa.Column('introduce_doc_id', sa.Integer(), nullable=True),
    sa.Column('received_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['introduce_doc_id'], ['introduce_docs.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['code_orders.id'], ),
    sa.ForeignKeyConstraint(['supply_id'], ['supplies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cis')
    )
    _create_index('mark_codes', 'ix_mark_codes_gtin', ['gtin'])
    _create_index('mark_codes', 'ix_mark_codes_order_id', ['order_id'])
    _create_index('mark_codes', 'ix_mark_codes_status', ['status'])
    _create_index('mark_codes', 'ix_mark_codes_supply_id', ['supply_id'])

    # ADD COLUMN на SQLite таблицу не перестраивает — batch не нужен.
    if not _has_column('supplies', 'intro_attrs'):
        op.add_column('supplies', sa.Column('intro_attrs', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('supplies', schema=None) as batch_op:
        batch_op.drop_column('intro_attrs')

    with op.batch_alter_table('mark_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mark_codes_supply_id'))
        batch_op.drop_index(batch_op.f('ix_mark_codes_status'))
        batch_op.drop_index(batch_op.f('ix_mark_codes_order_id'))
        batch_op.drop_index(batch_op.f('ix_mark_codes_gtin'))

    op.drop_table('mark_codes')
    with op.batch_alter_table('introduce_docs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_introduce_docs_supply_id'))
        batch_op.drop_index(batch_op.f('ix_introduce_docs_status'))

    op.drop_table('introduce_docs')
    with op.batch_alter_table('code_orders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_code_orders_supply_id'))
        batch_op.drop_index(batch_op.f('ix_code_orders_status'))

    op.drop_table('code_orders')
