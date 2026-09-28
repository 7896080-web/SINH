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


def test_a_mirror_pointing_at_the_backup_folder_is_refused(live_db, monkeypatch):
    """«Вторая площадка», совпадающая с первой, — не площадка.

    Отказ диска унесёт обе разом, то есть защита не работает вовсе. А выглядит
    это исправнее исправного: файл «доезжает» (переписывает сам себя), ошибки
    нет, `mirror_error` пуст, находка отчёта молчит, а скрипт бодро печатает
    «зеркало: <путь>». Сличить два пути в разных разделах страницы — занятие
    для того, кто и не ошибётся.
    """
    monkeypatch.setenv("BACKUP_MIRROR_DIR", str(live_db / "backups"))

    result = backup.make_backup()

    assert result.ok, "сам бэкап этим не портится — копия снята и проверена"
    assert "папка: указана на каталог самих копий" in result.mirror_error


def test_the_manual_script_says_when_there_is_no_second_site(live_db, monkeypatch,
                                                             capsys):
    """Молчание не должно означать «всё хорошо».

    Скрипт запускают именно чтобы УБЕДИТЬСЯ: ненастроенная площадка обязана
    выглядеть иначе, чем настроенная и работающая.
    """
    import importlib

    monkeypatch.delenv("BACKUP_MIRROR_DIR", raising=False)
    monkeypatch.delenv("BACKUP_RCLONE_REMOTE", raising=False)

    module = importlib.import_module("scripts.backup_db")
    assert module.main() == 0

    printed = capsys.readouterr().out
    assert "зеркало (папка): не настроено" in printed
    assert "зеркало (облако): не настроено" in printed
    assert "второй площадки нет" in printed


def test_the_manual_script_names_both_second_sites(live_db, monkeypatch, capsys):
    """Настроенное облако обязано быть НАЗВАНО.

    Раньше про него не было ни слова: молчание одинаково значило «выгрузили» и
    «выгружать некуда».
    """
    import importlib
    import subprocess

    monkeypatch.setenv("BACKUP_RCLONE_REMOTE", "yandex:backups")
    monkeypatch.setenv("BACKUP_RCLONE_EXE", str(live_db / "rclone.exe"))
    monkeypatch.setenv("BACKUP_RCLONE_CONFIG", str(live_db / "rclone.conf"))

    def fake_run(command, capture_output=True, text=True, timeout=None):
        sub = command[command.index("--log-level") + 2]
        if sub == "lsjson":
            name = os.path.basename(command[-1])
            # Отвечаем так, будто на той стороне лежит ровно то, что послали.
            import json as _json
            src = live_db / "backups"
            files = sorted(src.glob("sync_admin-*.db"))
            return subprocess.CompletedProcess(
                command, 0,
                _json.dumps([{"Name": f.name, "Size": f.stat().st_size}
                             for f in files]), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    module = importlib.import_module("scripts.backup_db")
    assert module.main() == 0

    printed = capsys.readouterr().out
    assert "зеркало (облако): yandex:backups" in printed
    assert "доставлена и проверена" in printed


def test_one_broken_site_does_not_read_as_no_site_at_all(live_db, monkeypatch, capsys):
    """Площадок ДВЕ, и отказ одной не значит, что не сработала вторая.

    22.09 на бою это и вышло: папка была задана в каталог самих копий и
    справедливо отвергнута, а облако приняло копию и подтвердило её чтением.
    Скрипт при этом печатал «копия не доехала до зеркала» — то есть пугал
    человека там, где копия на самом деле лежала в облаке. Строка, которая
    врёт в безопасную сторону, приучает не верить ей и в опасную.
    """
    import importlib
    import json as _json
    import subprocess

    # Папка зеркала указывает в каталог копий — отказ.
    monkeypatch.setenv("BACKUP_MIRROR_DIR", str(live_db / "backups"))
    # Облако настроено и работает.
    monkeypatch.setenv("BACKUP_RCLONE_REMOTE", "yandex:backups")
    monkeypatch.setenv("BACKUP_RCLONE_EXE", str(live_db / "rclone.exe"))
    monkeypatch.setenv("BACKUP_RCLONE_CONFIG", str(live_db / "rclone.conf"))

    def fake_run(command, capture_output=True, text=True, timeout=None):
        sub = command[command.index("--log-level") + 2]
        if sub == "lsjson":
            files = sorted((live_db / "backups").glob("sync_admin-*.db"))
            return subprocess.CompletedProcess(
                command, 0,
                _json.dumps([{"Name": f.name, "Size": f.stat().st_size}
                             for f in files]), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    module = importlib.import_module("scripts.backup_db")
    assert module.main() == 0

    printed = capsys.readouterr().out
    assert "папка:" in printed, "жалоба обязана называть свою площадку"
    assert "не доехала" not in printed
    assert "при этом доставлено и проверено: облако" in printed
