"""Миграции: доходят до конца на пустой базе, на базе, уже созданной
приложением (`create_all` при старте), и после собственного обрыва; схема
после них совпадает с моделями."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def _alembic(db_path: Path, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db_path}")
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=HERE, env=env,
                          capture_output=True, text=True, timeout=120)


def test_upgrade_on_empty_database_matches_models(tmp_path):
    db = tmp_path / "m.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    check = _alembic(db, "check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_upgrade_survives_tables_created_by_the_app(tmp_path):
    db = tmp_path / "m.db"
    code = ("import os; os.environ['DATABASE_URL']=%r; "
            "from priceapp.database import Base, engine; import priceapp.models; "
            "Base.metadata.create_all(bind=engine)") % f"sqlite:///{db}"
    env = dict(os.environ)
    assert subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env).returncode == 0
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr


def test_upgrade_survives_its_own_interruption(tmp_path):
    db = tmp_path / "m.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    con = sqlite3.connect(db)
    con.execute("DELETE FROM alembic_version")    # версия не сдвинулась, таблицы остались
    con.commit()
    con.close()
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
