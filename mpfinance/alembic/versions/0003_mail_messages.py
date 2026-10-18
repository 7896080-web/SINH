"""Письма с отчётами, разобранные с почты (недельные отчёты Lamoda).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18 09:21:24.815873

"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('mail_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.String(length=255), nullable=False),
    sa.Column('received_at', sa.DateTime(), nullable=True),
    sa.Column('sender', sa.String(length=255), nullable=True),
    sa.Column('subject', sa.String(length=500), nullable=True),
    sa.Column('attachments', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('processed_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id')
    )


def downgrade():
    op.drop_table('mail_messages')
