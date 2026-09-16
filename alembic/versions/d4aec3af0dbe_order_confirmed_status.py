"""order confirmed status

Revision ID: d4aec3af0dbe
Revises: 715e7b25bb23
Create Date: 2026-09-12 19:08:25.739724

"""
from alembic import op
import sqlalchemy as sa


revision = 'd4aec3af0dbe'
down_revision = '715e7b25bb23'
branch_labels = None
depends_on = None


def upgrade():
    # Добавляем значение 'confirmed' в enum статуса заказа. На SQLite это
    # CHECK-ограничение — пересоздаём колонку в batch-режиме с новым набором.
    with op.batch_alter_table('processed_orders', schema=None) as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('processed', 'cancelled', name='orderprocessstatus'),
            type_=sa.Enum('processed', 'confirmed', 'cancelled', name='orderprocessstatus'),
            existing_nullable=False,
        )


def downgrade():
    with op.batch_alter_table('processed_orders', schema=None) as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('processed', 'confirmed', 'cancelled', name='orderprocessstatus'),
            type_=sa.Enum('processed', 'cancelled', name='orderprocessstatus'),
            existing_nullable=False,
        )
