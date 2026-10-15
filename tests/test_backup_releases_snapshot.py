"""Долгая выгрузка не держит читающий снимок базы.

Копия снимается на ЖИВОЙ базе, и это главное свойство всего механизма: службы
останавливать нельзя, продажи идут. Но сессия, из которой прочитали настройки,
держит открытую транзакцию до первого `commit`/`rollback`, а в WAL живой снимок
не даёт чекпойнту перенести журнал в базу. Писателям он при этом не мешает —
поэтому беда тихая: остатки синхронизируются дальше, ошибок нет, а `-wal` растёт
всё время копирования и выгрузки.

Время это не секунды. Копия в 305 МБ на медленном канале уезжает десятками
минут (у WebDAV Яндекса замерено 111 КиБ/с — сорок шесть минут), и предел
выгрузки у нас сознательно поставлен в четыре часа: столько же держался бы и
снимок.
"""
import json
import subprocess

import pytest

from app import backup
from app.models import AuditLog
from app.timeutils import now_utc


class Watcher:
    """Подставной rclone, который на каждом вызове смотрит на сессию."""

    def __init__(self, db, listing=None):
        self.db = db
        self.listing = listing if listing is not None else []
        self.in_transaction: dict[str, bool] = {}

    def __call__(self, command, capture_output=True, timeout=None, **kw):
        sub = command[command.index("--log-level") + 2]
        self.in_transaction[sub] = self.db.in_transaction()
        if sub == "lsjson":
            return subprocess.CompletedProcess(command, 0,
                                               json.dumps(self.listing), "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture()
def copy(tmp_path):
    path = tmp_path / "copies" / "sync_admin-20261015-120000.db"
    path.parent.mkdir()
    path.write_bytes(b"x" * 1024)
    return path


@pytest.fixture()
def cloud(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_RCLONE_REMOTE", "hbox:sync_admin_backups")
    monkeypatch.setenv("BACKUP_RCLONE_EXE", str(tmp_path / "rclone.exe"))
    monkeypatch.setenv("BACKUP_RCLONE_CONFIG", str(tmp_path / "rclone.conf"))


def test_the_upload_runs_without_an_open_read_snapshot(db, copy, cloud,
                                                       monkeypatch):
    """Самое долгое место бэкапа — и до правки оно шло с открытым снимком."""
    watcher = Watcher(db, listing=[{"Name": copy.name, "Size": 1024}])
    monkeypatch.setattr(subprocess, "run", watcher)

    assert backup.upload_to_remote(copy, db=db) == ""

    assert watcher.in_transaction["copyto"] is False, (
        "выгрузка идёт с открытым читающим снимком: журнал WAL не сможет "
        "чекпойнтиться все четыре часа отведённого ей предела")


def test_reading_the_settings_really_does_open_a_transaction(db, cloud,
                                                             monkeypatch):
    """Обратная проверка: без отпускания снимок БЫЛ БЫ открыт.

    Без неё тест выше молчал бы и на сессии, которая транзакцию вообще не
    открывает, — то есть проверял бы не то, что думает."""
    monkeypatch.setattr(backup, "release_read_snapshot", lambda db_: None)
    watcher = Watcher(db, listing=[])
    monkeypatch.setattr(subprocess, "run", watcher)

    backup.upload_to_remote(backup.Path("нет-такого.db"), db=db)

    assert watcher.in_transaction["copyto"] is True, (
        "чтение настроек не открыло транзакцию — значит и отпускать нечего, "
        "и соседний тест ничего не проверяет")


def test_the_folder_mirror_also_lets_go(db, copy, tmp_path, monkeypatch):
    """Копия на сетевую папку идёт минутами — то же правило."""
    mirror = tmp_path / "mirror"
    monkeypatch.setenv("BACKUP_MIRROR_DIR", str(mirror))

    seen = {}
    real_copyfile = backup.shutil.copyfile

    def watched(src, dst, *a, **kw):
        seen["in_transaction"] = db.in_transaction()
        return real_copyfile(src, dst, *a, **kw)

    monkeypatch.setattr(backup.shutil, "copyfile", watched)

    assert backup.mirror_backup(copy, db=db) == ""
    assert seen["in_transaction"] is False


def test_the_session_still_works_after_letting_go(db, copy, cloud, monkeypatch):
    """Сессия обязана остаться рабочей.

    Сразу после выгрузки по ней читаются сроки хранения копий в облаке, а
    задание потом пишет ею heartbeat. Отпускание — это конец транзакции, а не
    конец сессии."""
    watcher = Watcher(db, listing=[{"Name": copy.name, "Size": 1024}])
    monkeypatch.setattr(subprocess, "run", watcher)

    backup.upload_to_remote(copy, db=db)

    db.add(AuditLog(actor="admin", action="после-выгрузки", details=""))
    db.commit()
    assert db.query(AuditLog).count() == 1


def test_uncommitted_work_is_never_thrown_away(db, copy, cloud, monkeypatch):
    """Чужую незакоммиченную работу не выбрасываем МОЛЧА.

    `rollback` на сессии с несохранёнными изменениями стёр бы их без следа, а
    это хуже растущего журнала. Сегодня такого вызывающего нет, но цена ошибки
    здесь — потерянная чужая запись, и платить её однажды нельзя."""
    watcher = Watcher(db, listing=[{"Name": copy.name, "Size": 1024}])
    monkeypatch.setattr(subprocess, "run", watcher)

    db.add(AuditLog(actor="admin", action="моя-работа", details="",
                    created_at=now_utc()))
    backup.upload_to_remote(copy, db=db)

    db.commit()
    assert db.query(AuditLog).filter(AuditLog.action == "моя-работа").count() == 1, (
        "выгрузка откатила чужую незакоммиченную работу")


def test_work_already_flushed_is_not_thrown_away_either(db, copy, cloud,
                                                        monkeypatch):
    """Сторожей ДВА, и второй — про `flush()`.

    После него работа для сессии «уже записана»: `db.new` пуст, `db.dirty`
    пуст, — а `rollback` выбросил бы её молча, то есть ровно тем способом, от
    которого сторож и заведён. Спрашиваем у драйвера: у pysqlite
    `in_transaction` ложно после `SELECT` и истинно после любой записи."""
    watcher = Watcher(db, listing=[{"Name": copy.name, "Size": 1024}])
    monkeypatch.setattr(subprocess, "run", watcher)

    db.add(AuditLog(actor="admin", action="уже-сброшена", details="",
                    created_at=now_utc()))
    db.flush()
    assert not db.new and not db.dirty, "flush не сработал — тест проверяет не то"

    backup.upload_to_remote(copy, db=db)

    db.commit()
    assert db.query(AuditLog).filter(
        AuditLog.action == "уже-сброшена").count() == 1, (
        "выгрузка откатила работу, уже отданную flush()")


def test_no_session_is_not_an_error(copy, cloud, monkeypatch):
    """`scripts/backup_db.py` зовёт бэкап БЕЗ сессии — ровно тогда, когда с
    базой что-то не так. Отпускать нечего, и падать тут нельзя."""
    watcher = Watcher.__new__(Watcher)
    watcher.db = None
    watcher.listing = [{"Name": copy.name, "Size": 1024}]
    watcher.in_transaction = {}

    def run(command, capture_output=True, timeout=None, **kw):
        sub = command[command.index("--log-level") + 2]
        if sub == "lsjson":
            return subprocess.CompletedProcess(
                command, 0, json.dumps(watcher.listing), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert backup.upload_to_remote(copy, db=None) == ""
