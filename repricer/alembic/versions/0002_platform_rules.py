"""Правила цены — у площадки, а не у кабинета; текущие цены с площадок

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02

Правило и комиссия переезжают с кабинета на площадку: у WB три ИП, условия у
них одни, и три карточки с одинаковыми числами только множили места, где они
разойдутся. Существующее правило переносится от первого кабинета площадки.

Каждый шаг спрашивает, не сделан ли он уже: на SQLite alembic упавшую
миграцию не откатывает, и повтор иначе падал бы навсегда.
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _tables():
    return set(_insp().get_table_names())


def _has_column(table, column):
    return column in {c["name"] for c in _insp().get_columns(table)}


def _add_column(table, column):
    if not _has_column(table, column.name):
        op.add_column(table, column)


def upgrade():
    if 'platform_rules' not in _tables():
        op.create_table('platform_rules',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('platform', sa.String(length=8), nullable=False),
            sa.Column('commission_percent', sa.Numeric(precision=6, scale=2), nullable=True),
            sa.Column('markup_coef', sa.Numeric(precision=7, scale=3), nullable=False),
            sa.Column('round_step', sa.Integer(), nullable=False),
            sa.Column('round_minus', sa.Integer(), nullable=False),
            sa.Column('min_markup_coef', sa.Numeric(precision=7, scale=3), nullable=False),
            sa.Column('max_change_percent', sa.Numeric(precision=7, scale=2), nullable=False),
            sa.Column('base_platform', sa.String(length=8), nullable=True),
            sa.Column('base_coef', sa.Numeric(precision=7, scale=3), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('platform'))

    bind = op.get_bind()
    has_commission = _has_column('accounts', 'commission_percent')
    if 'price_rules' in _tables():
        # Перенос: правило и комиссия первого кабинета площадки. Площадки, у
        # которых правило уже есть (повтор после обрыва), не трогаем.
        done = {r[0] for r in bind.execute(sa.text("SELECT platform FROM platform_rules"))}
        commission = "a.commission_percent" if has_commission else "NULL"
        rows = bind.execute(sa.text(
            f"SELECT a.platform, {commission}, r.markup_coef, r.round_step, r.round_minus, "
            "r.min_markup_coef, r.max_change_percent "
            "FROM accounts a LEFT JOIN price_rules r ON r.account_id = a.id ORDER BY a.id")).fetchall()
        for platform, comm, coef, step, minus, floor, max_change in rows:
            if platform in done:
                continue
            done.add(platform)
            bind.execute(sa.text(
                "INSERT INTO platform_rules (platform, commission_percent, markup_coef, round_step, "
                "round_minus, min_markup_coef, max_change_percent, updated_at) "
                "VALUES (:p, :c, :k, :s, :m, :f, :x, CURRENT_TIMESTAMP)"),
                dict(p=platform, c=comm, k=coef if coef is not None else 1, s=step or 1, m=minus or 0,
                     f=floor if floor is not None else 1, x=max_change if max_change is not None else 20))
        op.drop_table('price_rules')

    _add_column('accounts', sa.Column('prices_loaded_at', sa.DateTime(), nullable=True))
    _add_column('accounts', sa.Column('prices_note', sa.Text(), nullable=False, server_default=''))
    # Уборка временной таблицы batch — ДО вопроса «сделано ли уже»: обрыв после
    # перестройки, но до сдвига версии, оставил бы её навсегда.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_accounts")
    if _has_column('accounts', 'commission_percent'):
        with op.batch_alter_table('accounts') as batch:
            batch.drop_column('commission_percent')

    _add_column('platform_items', sa.Column('current_price', sa.Integer(), nullable=True))
    _add_column('platform_items', sa.Column('current_sale_price', sa.Integer(), nullable=True))
    _add_column('platform_items', sa.Column('price_loaded_at', sa.DateTime(), nullable=True))


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
