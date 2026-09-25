"""Возвраты с площадок и роли пользователей.

Что здесь важно и чего автогенерация не делает.

**`users.role` — с `server_default`.** Колонка NOT NULL без умолчания роняет
миграцию на первой же существующей строке, а строки там есть всегда: без
пользователя в админку не войти. Умолчание `admin` выбрано намеренно: молча
отнять доступ у живого человека хуже, чем дать лишний, а сузить его потом —
одна команда.

**`ftp_tasks` перестраивается batch-блоком**, и это самый долгий шаг: таблица
на боевом сервере в десятки тысяч строк, службы при этом живые. Обрыв на нём
оставил бы `_alembic_tmp_ftp_tasks`, и повторный `alembic upgrade head` падал бы
на «table … already exists» НАВСЕГДА: продолжить нечем, повторить нечем, службы
не перезапущены. Поэтому каждый необратимый шаг спрашивает, не сделан ли он уже.

Зачем вообще трогать `ftp_tasks`. У возврата нет кабинета: оприходование идёт на
ЦС, ИП к документу отношения не имеет, а на приёмке кабинет и неизвестен.
Площадка при этом нужна — по ней выбирается склад-источник, — поэтому она
приходит своим полем.
"""
from alembic import op
import sqlalchemy as sa


revision = 'bd8f521a8957'
down_revision = '930c4c4db5e9'
branch_labels = None
depends_on = None

RETURN_STATUS = sa.Enum('accepted', 'cleaning', 'repack', 'held', 'awaiting_1c',
                        'back_to_sale', 'rejected_1c', 'scrapped', name='returnstatus')
SCRAP_REASON = sa.Enum('defect', 'worn', 'swapped', 'illiquid', name='scrapreason')
PLATFORM = sa.Enum('wb', 'ozon', 'kit', name='platform')
USER_ROLE = sa.Enum('admin', 'warehouse', name='userrole')


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _has_column(bind, table: str, column: str) -> bool:
    if not _has_table(bind, table):
        return False
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _drop_tmp(bind, table: str) -> None:
    """Остаток оборванного batch-прогона. Без этого повтор падает навсегда."""
    op.execute(sa.text(f'DROP TABLE IF EXISTS _alembic_tmp_{table}'))


def upgrade():
    bind = op.get_bind()

    if not _has_table(bind, 'return_items'):
        op.create_table(
            'return_items',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('barcode', sa.String(length=64), nullable=False),
            sa.Column('uid_1c', sa.String(length=36), nullable=True),
            sa.Column('platform', PLATFORM, nullable=False),
            sa.Column('status', RETURN_STATUS, nullable=False),
            sa.Column('status_changed_at', sa.DateTime(), nullable=False),
            sa.Column('scrap_reason', SCRAP_REASON, nullable=True),
            sa.Column('note', sa.String(length=255), nullable=True),
            sa.Column('ftp_task_id', sa.Integer(), nullable=True),
            sa.Column('is_test', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.ForeignKeyConstraint(['ftp_task_id'], ['ftp_tasks.id'], ),
            sa.PrimaryKeyConstraint('id'),
        )

    existing = {i['name'] for i in sa.inspect(bind).get_indexes('return_items')}
    for name, cols in (('ix_return_items_barcode', ['barcode']),
                       ('ix_return_items_created_at', ['created_at']),
                       ('ix_return_items_platform', ['platform']),
                       ('ix_return_items_status', ['status']),
                       ('ix_return_items_uid_1c', ['uid_1c'])):
        if name not in existing:
            op.create_index(name, 'return_items', cols, unique=False)

    if not _has_table(bind, 'return_item_log'):
        op.create_table(
            'return_item_log',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('return_id', sa.Integer(), nullable=False),
            sa.Column('at', sa.DateTime(), nullable=False),
            sa.Column('from_status', RETURN_STATUS, nullable=True),
            sa.Column('to_status', RETURN_STATUS, nullable=False),
            sa.Column('note', sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(['return_id'], ['return_items.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
    existing = {i['name'] for i in sa.inspect(bind).get_indexes('return_item_log')}
    if 'ix_return_item_log_return_id' not in existing:
        op.create_index('ix_return_item_log_return_id', 'return_item_log',
                        ['return_id'], unique=False)

    # Самый долгий шаг: перестройка `ftp_tasks` при живых службах.
    if not _has_column(bind, 'ftp_tasks', 'platform'):
        _drop_tmp(bind, 'ftp_tasks')
        with op.batch_alter_table('ftp_tasks', schema=None) as batch_op:
            batch_op.add_column(sa.Column('platform', PLATFORM, nullable=True))
            batch_op.alter_column('account_id', existing_type=sa.INTEGER(), nullable=True)

    if not _has_column(bind, 'users', 'role'):
        _drop_tmp(bind, 'users')
        with op.batch_alter_table('users', schema=None) as batch_op:
            # `server_default` обязателен: без него NOT NULL роняет миграцию на
            # первой же существующей строке, а пользователь в базе есть всегда.
            batch_op.add_column(sa.Column('role', USER_ROLE, nullable=False,
                                          server_default='admin'))


def downgrade():
    bind = op.get_bind()

    if _has_column(bind, 'users', 'role'):
        _drop_tmp(bind, 'users')
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.drop_column('role')

    if _has_column(bind, 'ftp_tasks', 'platform'):
        _drop_tmp(bind, 'ftp_tasks')
        with op.batch_alter_table('ftp_tasks', schema=None) as batch_op:
            batch_op.alter_column('account_id', existing_type=sa.INTEGER(), nullable=False)
            batch_op.drop_column('platform')

    if _has_table(bind, 'return_item_log'):
        op.drop_table('return_item_log')
    if _has_table(bind, 'return_items'):
        op.drop_table('return_items')
