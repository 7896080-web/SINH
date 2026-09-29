"""Входящий файл поставки хранится при ней

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29 12:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def _has_column(table, column):
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Переживает собственный обрыв и колонку, созданную приложением (create_all).
    # ADD COLUMN на SQLite таблицу не перестраивает — batch не нужен.
    if not _has_column('supplies', 'source_file'):
        op.add_column('supplies', sa.Column('source_file', sa.LargeBinary(), nullable=True))


def downgrade():
    with op.batch_alter_table('supplies') as batch:
        batch.drop_column('source_file')
