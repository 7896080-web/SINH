import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://sync_user:sync_password@localhost:5432/sync_admin",
)

_is_sqlite = DATABASE_URL.startswith("sqlite")

# SQLite на одной машине с двумя процессами (веб + планировщик пишут в один файл):
# check_same_thread=False — соединение переживает потоки; timeout — ждём снятия
# блокировки, а не падаем сразу.
_connect_args = {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        # WAL — читатели не блокируют писателя и наоборот; busy_timeout — ждать
        # блокировку до 30с; synchronous=NORMAL — безопасно при WAL и быстрее.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
