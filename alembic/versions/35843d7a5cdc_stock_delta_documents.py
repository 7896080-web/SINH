"""stock_delta_documents — защита от повторного применения дельты остатков ЦС

Revision ID: 35843d7a5cdc
Revises: 7c4e1b90a2d5
Create Date: 2026-09-19 00:17:33.247823

Autogenerate дополнительно предложил alter_column на `ftp_tasks.status`
(VARCHAR(7) → Enum с новым значением 'no_document'). Эта правка УБРАНА руками:
на SQLite длина VARCHAR не проверяется, значение и так пишется как есть, а
batch_alter_table пересобрал бы живую таблицу заданий целиком — копирование,
удаление, переименование — ради нуля пользы. Значение перечисления уже учтено
миграцией 7c4e1b90a2d5, где работа делается только на PostgreSQL.
"""
from alembic import op
import sqlalchemy as sa


revision = '35843d7a5cdc'
down_revision = '7c4e1b90a2d5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'stock_delta_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.String(length=128), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=True),
        sa.Column('applied_at', sa.DateTime(), nullable=False),
        sa.Column('lines', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('stock_delta_documents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stock_delta_documents_document_id'),
                              ['document_id'], unique=True)


def downgrade():
    with op.batch_alter_table('stock_delta_documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stock_delta_documents_document_id'))
    op.drop_table('stock_delta_documents')
