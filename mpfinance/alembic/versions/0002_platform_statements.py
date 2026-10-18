"""Свод отчётов площадки (список финансовых отчётов WB) — эталон для сверки.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18 09:13:21.923515

"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('platform_statements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('report_number', sa.String(length=64), nullable=False),
    sa.Column('report_kind', sa.String(length=32), nullable=True),
    sa.Column('legal_entity', sa.String(length=255), nullable=True),
    sa.Column('period_from', sa.Date(), nullable=False),
    sa.Column('period_to', sa.Date(), nullable=False),
    sa.Column('formed_at', sa.Date(), nullable=True),
    sa.Column('sale_kop', sa.BigInteger(), nullable=True),
    sa.Column('for_pay_kop', sa.BigInteger(), nullable=True),
    sa.Column('delivery_kop', sa.BigInteger(), nullable=True),
    sa.Column('storage_kop', sa.BigInteger(), nullable=True),
    sa.Column('acceptance_kop', sa.BigInteger(), nullable=True),
    sa.Column('penalty_kop', sa.BigInteger(), nullable=True),
    sa.Column('deduction_kop', sa.BigInteger(), nullable=True),
    sa.Column('total_kop', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('loaded_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'report_number', name='uq_statement_number')
    )
    with op.batch_alter_table('platform_statements', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_platform_statements_period_from'), ['period_from'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_statements_platform'), ['platform'], unique=False)



def downgrade():
    with op.batch_alter_table('platform_statements', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_platform_statements_platform'))
        batch_op.drop_index(batch_op.f('ix_platform_statements_period_from'))

    op.drop_table('platform_statements')
