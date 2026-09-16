"""product broadcast_enabled + transmit_override + broadcast_active_since

Revision ID: f2a9c1d7b0e4
Revises: d4aec3af0dbe
Create Date: 2026-09-13 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f2a9c1d7b0e4'
down_revision = 'd4aec3af0dbe'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        # Ручной «передаваемый остаток» (override), NULL = считать автоматически.
        batch_op.add_column(sa.Column('transmit_override', sa.Integer(), nullable=True))
        # Трансляция на площадки — ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНА (безопасный дефолт:
        # новый/существующий SKU не льёт остаток на площадки, пока не включат).
        batch_op.add_column(sa.Column('broadcast_enabled', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        # Дата «активно с» — для аудита и backfill.
        batch_op.add_column(sa.Column('broadcast_active_since', sa.Date(), nullable=True))

    # Ручная пауза трансляции на кабинет (по площадке / глобально в UI).
    with op.batch_alter_table('platform_accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dispatch_enabled', sa.Boolean(), nullable=False,
                                      server_default=sa.true()))


def downgrade():
    with op.batch_alter_table('platform_accounts', schema=None) as batch_op:
        batch_op.drop_column('dispatch_enabled')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('broadcast_active_since')
        batch_op.drop_column('broadcast_enabled')
        batch_op.drop_column('transmit_override')
