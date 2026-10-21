import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from forecast.db import make_engine

PROJECT = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    cfg = Config(str(PROJECT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory) -> Path:
    """Одна база, поднятая настоящими миграциями; каждый тест получает её копию."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    command.upgrade(alembic_config(f"sqlite:///{path}"), "head")
    return path


@pytest.fixture
def db_url(migrated_template, tmp_path) -> str:
    path = tmp_path / "test.db"
    shutil.copy(migrated_template, path)
    return f"sqlite:///{path}"


@pytest.fixture
def engine(db_url):
    eng = make_engine(db_url)
    yield eng
    eng.dispose()
