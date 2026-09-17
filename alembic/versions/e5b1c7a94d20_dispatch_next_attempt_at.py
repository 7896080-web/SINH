"""dispatch_queue.next_attempt_at (пауза между повторами отправки)

Revision ID: e5b1c7a94d20
Revises: c1f5a9d2e3b4
Create Date: 2026-09-17 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e5b1c7a94d20'
down_revision = 'c1f5a9d2e3b4'
branch_labels = None
depends_on = None


def upgrade():
    # Момент, раньше которого повторять отправку нельзя. NULL = можно сейчас,
    # поэтому server_default не нужен: все существующие записи очереди остаются
    # доступными к отправке ровно как раньше.
    with op.batch_alter_table('dispatch_queue', schema=None) as batch_op:
        batch_op.add_column(sa.Column('next_attempt_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('dispatch_queue', schema=None) as batch_op:
        batch_op.drop_column('next_attempt_at')
