"""Копия базы: та самая, которой на боевом сервере не было вовсе.

Аудит 21.09: `deploy/backup.sh` есть, но это Linux-скрипт из другого варианта
развёртывания, а в `README_WINDOWS.md` слова «бэкап» нет. Боевую базу — 94 МБ,
в которых мэппинг баркодов, история проведения и задания 1С, — не копировал
никто. Ничего из этого не восстанавливается ни из 1С, ни с площадок.

Здесь закреплено то, без чего копия — не копия: она снимается на ЖИВОЙ базе
(останавливать службы нельзя, продажи идут), она проверяется сразу, и старые
копии не съедают диск, но и не выбрасывают историю целиком.
"""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app import backup
from app.timeutils import now_utc


@pytest.fixture
def source_db(tmp_path):
    """Маленькая база, похожая на настоящую в главном: WAL и таблица товаров."""
    path = tmp_path / "sync_admin.db"
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE products (uid_1c TEXT PRIMARY KEY, article TEXT)")
    con.executemany("INSERT INTO products VALUES (?,?)",
                    [(f"u{i}", f"ART{i}") for i in range(50)])
    con.commit()
    con.close()
    return path


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


# ------------------------------------------------------------- копия снимается

def test_a_backup_is_created_and_verified(tmp_path, source_db):
    result = backup.make_backup(_url(source_db), tmp_path / "backups")

    assert result.ok
    assert Path(result.path).exists()
    assert result.size_bytes > 0


def test_the_copy_holds_the_data(tmp_path, source_db):
    result = backup.make_backup(_url(source_db), tmp_path / "backups")

    con = sqlite3.connect(result.path)
    try:
        assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 50
    finally:
        con.close()


def test_the_source_is_not_touched(tmp_path, source_db):
    """Копия — операция чтения. Испортить оригинал ею нельзя ни при каких
    обстоятельствах: это ровно тот файл, который мы защищаем."""
    before = source_db.read_bytes()

    backup.make_backup(_url(source_db), tmp_path / "backups")

    assert source_db.read_bytes() == before


def test_an_open_writer_does_not_break_the_backup(tmp_path, source_db):
    """Главное свойство: службы НЕ останавливаются. В базу в этот момент пишут
    и воркер, и веб — копия обязана сняться и быть согласованной.

    Обычное копирование файла тут и подводит: при живом WAL часть транзакций
    лежит в `-wal`, и копия одного `.db` может не открыться вовсе.
    """
    writer = sqlite3.connect(source_db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO products VALUES ('u999','ПОКА ПИШУТ')")
    writer.commit()

    result = backup.make_backup(_url(source_db), tmp_path / "backups")
    writer.close()

    assert result.ok
    con = sqlite3.connect(result.path)
    try:
        assert con.execute(
            "SELECT article FROM products WHERE uid_1c='u999'").fetchone()[0] == "ПОКА ПИШУТ"
    finally:
        con.close()


def test_the_backup_dir_is_created(tmp_path, source_db):
    target = tmp_path / "нет" / "такого" / "каталога"

    assert backup.make_backup(_url(source_db), target).ok
    assert target.exists()


# ------------------------------------------------------- честно про неудачу

def test_a_missing_database_is_an_error_not_a_crash(tmp_path):
    result = backup.make_backup(_url(tmp_path / "нетбазы.db"), tmp_path / "b")

    assert not result.ok
    assert "нет" in result.error


def test_a_non_sqlite_database_is_refused(tmp_path):
    """У PostgreSQL свой механизм. Делать вид, что мы прикрыли и его, опаснее,
    чем честно ничего не делать."""
    result = backup.make_backup("postgresql://u:p@localhost/db", tmp_path / "b")

    assert not result.ok
    assert "не SQLite" in result.error


def test_a_corrupted_copy_is_not_reported_as_ok(tmp_path, source_db, monkeypatch):
    """Непроверенная копия — это не копия, а надежда: о том, что она битая,
    узнают в тот единственный день, когда она нужна."""
    monkeypatch.setattr(backup, "_verify", lambda path: "integrity_check: malformed")

    result = backup.make_backup(_url(source_db), tmp_path / "b")

    assert not result.ok
    assert "не прошла проверку" in result.error


def test_a_failed_copy_is_left_on_disk(tmp_path, source_db, monkeypatch):
    """Битый файл вместе с ошибкой в журнале честнее пустого каталога, по
    которому не понять, была попытка или нет."""
    monkeypatch.setattr(backup, "_verify", lambda path: "плохо")

    result = backup.make_backup(_url(source_db), tmp_path / "b")

    assert Path(result.path).exists()


# --------------------------------------------------------------- сроки копий

def _fake(directory: Path, days_ago: int) -> Path:
    moment = now_utc() - timedelta(days=days_ago)
    path = directory / f"{backup.NAME_PREFIX}{moment:%Y%m%d-%H%M%S}.db"
    path.write_bytes(b"x")
    return path


def test_recent_copies_are_all_kept(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    kept = [_fake(tmp_path, i) for i in range(backup.KEEP_DAILY)]

    backup.prune(tmp_path)

    assert all(p.exists() for p in kept)


def test_old_copies_thin_out_to_one_a_week(tmp_path):
    """Порчу данных замечают и через месяц — но хранить ради этого тридцать
    ежедневных копий незачем."""
    for day in range(60):
        _fake(tmp_path, day)

    backup.prune(tmp_path)
    left = list(tmp_path.glob(f"{backup.NAME_PREFIX}*.db"))

    assert len(left) <= backup.KEEP_DAILY + backup.KEEP_WEEKLY
    assert len(left) > backup.KEEP_DAILY, "история за прошлые недели не должна пропадать"


def test_a_hand_made_copy_is_never_deleted(tmp_path):
    """В каталоге бэкапов может лежать копия, положенная человеком руками.
    Удалить её значит сделать ровно то, от чего этот модуль защищает."""
    mine = tmp_path / "перед_накатом.db"
    mine.write_bytes(b"x")
    for day in range(40):
        _fake(tmp_path, day)

    backup.prune(tmp_path)

    assert mine.exists()


def test_pruning_an_empty_dir_is_not_an_error(tmp_path):
    assert backup.prune(tmp_path) == 0


# ------------------------------------------------------------ «когда снимали»

def test_the_last_backup_is_reported(tmp_path):
    _fake(tmp_path, 5)
    newest = _fake(tmp_path, 1)

    moment, total = backup.last_backup(tmp_path)

    assert total == 2
    assert moment == backup._parse_moment(newest.name)


def test_no_backups_means_none_not_zero(tmp_path):
    """«Копий нет» и «копия от начала времён» — разные вещи, и отчёт по ним
    говорит разное."""
    moment, total = backup.last_backup(tmp_path)

    assert moment is None
    assert total == 0
