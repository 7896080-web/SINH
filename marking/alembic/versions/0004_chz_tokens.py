"""Токены входа в ЧЗ сертификатом — у организации

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30 20:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

COLUMNS = (
    ('chz_token_enc', sa.Text()),
    ('chz_token_until', sa.DateTime()),
    ('suz_token_enc', sa.Text()),
    ('suz_token_until', sa.DateTime()),
)


def _has_column(table, column):
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Переживает собственный обрыв и колонки, созданные приложением (create_all).
    for name, type_ in COLUMNS:
        if not _has_column('organizations', name):
            op.add_column('organizations', sa.Column(name, type_, nullable=True))


def downgrade():
    with op.batch_alter_table('organizations') as batch:
        for name, _ in reversed(COLUMNS):
            batch.drop_column(name)
