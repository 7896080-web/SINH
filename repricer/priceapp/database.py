import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./repricer.db")
_is_sqlite = DATABASE_URL.startswith("sqlite")

# Веб и фоновый поток пишут в один файл: блокировку ждём, а не падаем.
_connect_args = {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

# autoflush=False, как у sync_admin и «Маркировки»: объекты, добавленные в
# сессию, запрос не увидит до flush(). Тесты настроены так же.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# Кэш тяжёлых чтений В ПРЕДЕЛАХ одной транзакции (сборка сопоставления, справочник
# артикулов): страница «Внимание» собирала сопоставление каждого кабинета дважды.
# Любой коммит или откат кэш стирает — записанное следующим чтением будет видно.
def session_cache(db) -> dict:
    return db.info.setdefault("cache", {})


@event.listens_for(SessionLocal, "after_commit")
@event.listens_for(SessionLocal, "after_rollback")
def _drop_cache(session):
    session.info.pop("cache", None)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- версия данных для кэша расчёта цен -------------------------------------------
#
# «Цены товаров» пересчитывали весь каталог «где» на КАЖДЫЙ запрос: 6–8 с на
# страницу, поиск одного артикула столько же, строка «ok» — 12 с (расчёт в POST и
# ещё раз после редиректа). Кэш результата живёт до первой записи в таблицы, от
# которых зависит цена: любой коммит, тронувший их, — веб или фоновый поток, один
# процесс, — сдвигает версию, и следующий запрос считает заново. Таблицы, на цену
# не влияющие (журнал, отметки заданий, «Внимание»), версию не двигают: иначе
# фоновый поток сбрасывал бы кэш каждую минуту и ускорения не было бы вовсе.

_DATA_VERSION = [0]
_IGNORED_TABLES = {"audit_log", "worker_heartbeats", "users", "saved_filters", "onec_tasks", "api_credentials"}


def data_version() -> int:
    return _DATA_VERSION[0]


def _touches_prices(obj) -> bool:
    table = getattr(obj, "__tablename__", "")
    if table in _IGNORED_TABLES:
        return False
    if table == "settings":
        return str(getattr(obj, "key", "")).startswith("rate_")
    return True


@event.listens_for(SessionLocal, "after_flush")
def _note_price_writes(session, _ctx):
    if any(_touches_prices(o) for o in (*session.new, *session.dirty, *session.deleted)):
        session.info["prices_written"] = True


@event.listens_for(SessionLocal, "do_orm_execute")
def _note_bulk_writes(state):
    # query.update()/delete() мимо единицы работы — after_flush их не видит.
    if state.is_update or state.is_delete:
        state.session.info["prices_written"] = True


@event.listens_for(SessionLocal, "after_commit")
def _bump_version(session):
    if session.info.pop("prices_written", None):
        _DATA_VERSION[0] += 1


@event.listens_for(SessionLocal, "after_rollback")
def _forget_writes(session):
    session.info.pop("prices_written", None)


def wrote_prices(db) -> bool:
    """В текущей транзакции уже записано что-то, влияющее на цену (ещё без
    коммита) — кэшем пользоваться нельзя."""
    return bool(db.info.get("prices_written")) or bool(db.new or db.dirty or db.deleted)
