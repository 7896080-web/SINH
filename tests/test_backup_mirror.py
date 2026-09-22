"""Вторая площадка для копий: зачем через временное имя и почему сбой не роняет бэкап.

Зеркало заводится ровно против одного случая — отказ диска, на котором лежат И
база, И все копии. Поэтому проверяется не «скопировалось ли», а то, из-за чего
эта защита могла бы молча не работать.
"""
import os
import sqlite3
from datetime import timedelta

import pytest

from app import backup, report
from app.models import WorkerHeartbeat
from app.timeutils import now_utc


@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    """Настоящая SQLite-база на диске плюс пустые каталоги копий и зеркала."""
    source = tmp_path / "sync_admin.db"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE products (uid_1c TEXT)")
    con.execute("INSERT INTO products VALUES ('u1')")
    con.commit()
    con.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{source}")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_MIRROR_DIR", str(tmp_path / "mirror"))
    return tmp_path


def test_a_verified_copy_reaches_the_mirror(live_db):
    """Штатный случай: копия снята, проверена и лежит на обеих площадках."""
    result = backup.make_backup()

    assert result.ok and not result.mirror_error
    name = os.path.basename(result.path)
    mirrored = live_db / "mirror" / name
    assert mirrored.is_file()
    assert mirrored.stat().st_size == result.size_bytes


def test_the_mirror_never_sees_a_half_written_file(live_db, monkeypatch):
    """Под ФИНАЛЬНЫМ именем файл обязан появиться уже целым.

    Клиент облачной синхронизации смотрит за папкой и заливает всё, что в ней
    меняется. Копируй мы прямо в целевое имя — он отправил бы в облако
    недописанную базу. Поэтому копирование идёт во временное имя, а потом
    переименование, атомарное в пределах папки.
    """
    seen = []
    real_copy = backup.shutil.copyfile

    def watching_copy(src, dst):
        # Ровно то, что увидел бы клиент синхронизации в момент записи.
        seen.append(os.path.basename(dst))
        return real_copy(src, dst)

    monkeypatch.setattr(backup.shutil, "copyfile", watching_copy)

    result = backup.make_backup()

    assert result.ok
    assert seen and all(n.endswith(".part") for n in seen), \
        "копия писалась сразу под именем копии — облако залило бы огрызок"
    # И временный файл не остаётся: имя `.part` уборка не видит, он копился бы вечно.
    assert list((live_db / "mirror").glob("*.part")) == []


def test_the_staging_name_is_not_mistaken_for_a_backup(live_db):
    """`.part` не должен проходить за копию.

    Уборка ищет `sync_admin-*.db`, и оборванный кусок под правильным именем
    сошёл бы у неё и у `last_backup` за полноценную копию — ровно тот дефект,
    из-за которого сорвавшаяся попытка откладывается в `.bad`.
    """
    mirror = live_db / "mirror"
    mirror.mkdir()
    (mirror / "sync_admin-20260101-000000.db.part").write_bytes("огрызок".encode())

    moment, total = backup.last_backup(mirror)

    assert moment is None and total == 0


def test_an_unreachable_mirror_does_not_fail_the_backup(live_db, monkeypatch):
    """Локальная копия снята и прочитана — это успех.

    Объявить бэкап неудачным из-за недоступной сетевой папки значило бы поднять
    тревогу о том, чего не случилось, и приучить к ней.
    """
    def boom(src, dst):
        raise OSError("сетевой путь не найден")

    monkeypatch.setattr(backup.shutil, "copyfile", boom)

    result = backup.make_backup()

    assert result.ok, "бэкап обязан остаться успешным"
    assert "сетевой путь не найден" in result.mirror_error
    assert os.path.isfile(result.path)


def test_a_silent_mirror_becomes_a_finding(db):
    """Но и молчать нельзя: зеркало, о котором думают, что оно работает, хуже
    отсутствующего — случай, против которого оно заведено, снова не прикрыт."""
    db.add(WorkerHeartbeat(worker_name="backup", last_run_at=now_utc(),
                           last_success=True,
                           last_error="копия не доехала до зеркала: OSError"))
    db.commit()

    finding = report._check_backup_mirror(db)

    assert finding is not None
    assert "том же диске" in finding.consequence


def test_a_working_mirror_is_quiet(db):
    """Молчание на исправной системе — обязательное свойство отчёта."""
    db.add(WorkerHeartbeat(worker_name="backup", last_run_at=now_utc(),
                           last_success=True, last_error=None))
    db.commit()

    assert report._check_backup_mirror(db) is None


def test_no_mirror_configured_is_not_an_error(live_db, monkeypatch):
    """Зеркало необязательно: без него всё работает как раньше."""
    monkeypatch.delenv("BACKUP_MIRROR_DIR", raising=False)

    result = backup.make_backup()

    assert result.ok and result.mirror_error == ""
    assert backup.mirror_dir() is None


def test_the_mirror_keeps_its_own_depth(live_db):
    """Зеркало — вторая ПЛОЩАДКА, а не глубина хранения, но чистится по своему
    правилу: облако просторнее локального диска, и держать там ровно столько же
    копий незачем."""
    mirror = live_db / "mirror"
    mirror.mkdir()
    base = now_utc()
    for day in range(40):
        moment = base - timedelta(days=day)
        path = mirror / f"sync_admin-{moment:%Y%m%d-%H%M%S}.db"
        path.write_bytes(b"x")

    backup.prune(mirror, keep_daily=backup.MIRROR_KEEP_DAILY,
                 keep_weekly=backup.MIRROR_KEEP_WEEKLY)
    left = len(list(mirror.glob("sync_admin-*.db")))

    assert left > backup.KEEP_DAILY, "в зеркале держим глубже, чем локально"
    assert left <= backup.MIRROR_KEEP_DAILY + backup.MIRROR_KEEP_WEEKLY
