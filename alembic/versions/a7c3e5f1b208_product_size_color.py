"""product size and color

Revision ID: a7c3e5f1b208
Revises: f2a9c1d7b0e4
Create Date: 2026-09-14

Добавляет products.size и products.color — размер и цвет размер-цвет SKU
из выгрузки 1С (размер = характеристика, цвет = реквизит hiЦвет). Только для
отображения оператору на страницах товаров/остатков/маппинга/аномалий.
"""
from alembic import op
import sqlalchemy as sa

revision = "a7c3e5f1b208"
down_revision = "f2a9c1d7b0e4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("size", sa.String(length=32), nullable=True))
    op.add_column("products", sa.Column("color", sa.String(length=128), nullable=True))


def downgrade():
    op.drop_column("products", "color")
    op.drop_column("products", "size")
