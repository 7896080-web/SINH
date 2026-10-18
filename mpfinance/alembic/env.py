"""Миграции базы программы «Финансы площадок». Адрес базы — из DATABASE_URL (.env)."""
import finapp  # noqa: F401  — подхватывает .env
import finapp.models  # noqa: F401  — регистрирует таблицы
from alembic import context
from sqlalchemy import engine_from_config, pool

from finapp.database import Base, DATABASE_URL

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL)
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=DATABASE_URL, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
