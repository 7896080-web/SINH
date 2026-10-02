"""Цена = базовая (себестоимость × курс) × коэффициент артикула

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02

Базовая цена больше не хранится — она вычисляется из себестоимости и курса и
руками не правится; абсолютные цены площадки на товар заменены коэффициентом
на АРТИКУЛ (по площадке или по кабинету). Обе прежние таблицы удаляются.

Коэффициент площадки по умолчанию живёт в `platform_rules.base_coef`. Прежде
он значил «× к базовой цене», только если `base_platform = 'base'`; у
остальных правил (своё правило или цена от другой площадки) смысл другой, и
перенести его нельзя — такой коэффициент снимается: по площадке цены не будет,
пока его не зададут, вместо того чтобы молча посчитать её не тем числом.

Каждый шаг спрашивает, не сделан ли он уже: alembic на SQLite упавшую
миграцию не откатывает.
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'article_coefs' not in tables:
        op.create_table('article_coefs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('article', sa.String(length=200), nullable=False),
            sa.Column('platform', sa.String(length=8), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('coef', sa.Numeric(precision=7, scale=3), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(length=64), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('article', 'platform', 'account_id', name='uq_article_coef'))
    existing = {i["name"] for i in sa.inspect(bind).get_indexes('article_coefs')}
    if 'ix_article_coefs_article' not in existing:
        op.create_index('ix_article_coefs_article', 'article_coefs', ['article'])
    # Один оператор и только по строкам, где base_platform ещё задан, — повтор
    # после обрыва ничего не трогает (у правила без базы base_coef и так пуст:
    # прежняя проверка правила его обнуляла).
    bind.execute(sa.text(
        "UPDATE platform_rules SET "
        "base_coef = CASE WHEN base_platform = 'base' THEN base_coef ELSE NULL END, "
        "base_platform = NULL WHERE base_platform IS NOT NULL"))
    op.execute("DROP TABLE IF EXISTS base_prices")
    op.execute("DROP TABLE IF EXISTS platform_prices")


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
