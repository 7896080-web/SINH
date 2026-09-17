"""порог от даты: offset_base_date, offset_base_stock, fact_at_date

Порог трансляции перестал быть числом, которое оператор вбивает руками: теперь
он выводится из трёх величин на выбранную дату

    порог = остаток ЦС на дату − (факт на дату − бронь)

и по-прежнему записывается в products.broadcast_offset. Хранить исходные три
величины обязательно — без них порог нельзя пересчитать при смене брони.

Данные не трогаются: все три колонки nullable, у существующих товаров они NULL,
а NULL означает «порог живёт по-старому» — как введённое руками число или как
не заданный вовсе. Ни один настроенный товар поведения не меняет.

Revision ID: edffb59ad0db
Revises: 4a9993b59475
Create Date: 2026-09-17 15:51:13.726502

"""
from alembic import op
import sqlalchemy as sa


revision = 'edffb59ad0db'
down_revision = '4a9993b59475'
branch_labels = None
depends_on = None


def upgrade():
    # Все три nullable и без server_default: NULL здесь несёт смысл.
    # offset_base_stock: NULL — «1С ещё не ответила», 0 — «товара на дату не было».
    # Это разные состояния, поэтому ноль по умолчанию ставить нельзя.
    # fact_at_date: NULL — «оператор не вводил» (берём остаток ЦС на дату),
    # 0 — «на складе пусто». Тоже разное.
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('offset_base_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('offset_base_stock', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('fact_at_date', sa.Integer(), nullable=True))


def downgrade():
    # Откат теряет исходные три величины, но НЕ порог: broadcast_offset остаётся
    # с последним посчитанным значением и продолжает работать как ручной.
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('fact_at_date')
        batch_op.drop_column('offset_base_stock')
        batch_op.drop_column('offset_base_date')
