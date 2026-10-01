"""репрайсер и сопоставление артикулов

Чисто аддитивно: таблицы price_rules, product_prices, price_changes (репрайсер,
страница «Цены»), article_match_rules (страница «Сопоставление артикулов») и
nullable-колонки products.cost_price / cost_price_updated_at (себестоимость из
1С, вид цен «Цена СС») и platform_catalog_items.size (размер WB techSize —
заполнится при следующей загрузке спецификации). Существующие данные не
трогаются; откат сносит только новое (вместе с enum-типом в PostgreSQL).

Каждый шаг переживает СВОЙ обрыв (как и соседние миграции): Alembic на SQLite
упавшую миграцию не откатывает, и повторный `upgrade head` иначе встал бы на
«table already exists» навсегда. Поэтому таблицы и индексы создаются только
если их нет, колонки добавляются простым ADD COLUMN (без перестройки таблицы).

Revision ID: 7180736d14a3
Revises: a3cadcda0234
Create Date: 2026-10-01 21:44:32.003345

"""
from alembic import op
import sqlalchemy as sa


revision = '7180736d14a3'
down_revision = 'a3cadcda0234'
branch_labels = None
depends_on = None


TABLES = ("price_rules", "product_prices", "price_changes", "article_match_rules")


def _insp():
    return sa.inspect(op.get_bind())


def _tables() -> set:
    return set(_insp().get_table_names())


def _columns(table: str) -> set:
    return {c["name"] for c in _insp().get_columns(table)}


def _indexes(table: str) -> set:
    return {i["name"] for i in _insp().get_indexes(table)}


def upgrade():
    for table in TABLES + ("products", "platform_catalog_items"):
        op.execute(f"DROP TABLE IF EXISTS _alembic_tmp_{table}")
    have = _tables()

    if "price_changes" not in have:
        op.create_table('price_changes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uid_1c', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('cost_price', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('old_price', sa.Integer(), nullable=True),
        sa.Column('new_price', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('status', sa.Enum('proposed', 'blocked', 'approved', 'sent', 'error', 'rejected', name='pricechangestatus'), nullable=False),
        sa.Column('block_reason', sa.String(length=16), nullable=True),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('decided_by', sa.String(length=64), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('is_test', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
        sa.ForeignKeyConstraint(['uid_1c'], ['products.uid_1c'], ),
        sa.PrimaryKeyConstraint('id')
        )
    if "ix_price_changes_uid_1c" not in _indexes("price_changes"):
        op.create_index('ix_price_changes_uid_1c', 'price_changes', ['uid_1c'], unique=False)

    if "price_rules" not in have:
        op.create_table('price_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('markup_percent', sa.Numeric(precision=7, scale=2), nullable=False),
        sa.Column('fixed_add', sa.Integer(), nullable=False),
        sa.Column('round_step', sa.Integer(), nullable=False),
        sa.Column('round_minus', sa.Integer(), nullable=False),
        sa.Column('min_margin_percent', sa.Numeric(precision=7, scale=2), nullable=False),
        sa.Column('max_change_percent', sa.Numeric(precision=7, scale=2), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id')
        )

    if "product_prices" not in have:
        op.create_table('product_prices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uid_1c', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('manual_price', sa.Integer(), nullable=True),
        sa.Column('last_sent_price', sa.Integer(), nullable=True),
        sa.Column('last_sent_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
        sa.ForeignKeyConstraint(['uid_1c'], ['products.uid_1c'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('uid_1c', 'account_id', name='uq_price_product_account')
        )

    if "article_match_rules" not in have:
        op.create_table('article_match_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('kinds', sa.String(length=128), nullable=False),
        sa.Column('strip_prefix', sa.String(length=64), nullable=True),
        sa.Column('strip_suffix', sa.String(length=64), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['platform_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id')
        )

    # Простое ADD COLUMN (nullable, без значения по умолчанию) — SQLite делает его
    # на месте, без перестройки таблицы products на 150 тысяч строк.
    if "size" not in _columns("platform_catalog_items"):
        op.add_column('platform_catalog_items', sa.Column('size', sa.String(length=64), nullable=True))
    product_cols = _columns("products")
    if "cost_price" not in product_cols:
        op.add_column('products', sa.Column('cost_price', sa.Numeric(precision=12, scale=2), nullable=True))
    if "cost_price_updated_at" not in product_cols:
        op.add_column('products', sa.Column('cost_price_updated_at', sa.DateTime(), nullable=True))


def downgrade():
    for table in TABLES + ("products", "platform_catalog_items"):
        op.execute(f"DROP TABLE IF EXISTS _alembic_tmp_{table}")
    if {"cost_price", "cost_price_updated_at"} & _columns("products"):
        with op.batch_alter_table('products', schema=None) as batch_op:
            for col in ("cost_price_updated_at", "cost_price"):
                if col in _columns("products"):
                    batch_op.drop_column(col)
    if "size" in _columns("platform_catalog_items"):
        with op.batch_alter_table('platform_catalog_items', schema=None) as batch_op:
            batch_op.drop_column('size')
    have = _tables()
    for table in ("article_match_rules", "product_prices", "price_rules", "price_changes"):
        if table in have:
            op.drop_table(table)
    # В PostgreSQL enum — отдельный тип, drop_table его не убирает.
    sa.Enum(name='pricechangestatus').drop(op.get_bind(), checkfirst=True)
