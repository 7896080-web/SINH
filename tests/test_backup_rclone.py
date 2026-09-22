"""Копии в облако через rclone: проверяется не «вызвали ли», а доехало ли.

rclone взят вместо папки синхронизации ровно из-за одного свойства: у него
МОЖНО спросить, лежит ли копия на той стороне. У папки нельзя — там видно
только, что файл положен в папку, и отсутствие второй площадки выглядит точно
так же, как её наличие. Поэтому тесты здесь про проверку и про молчаливые
отказы, а не про формирование команды.
"""
import json
import subprocess

import pytest

from app import backup


class FakeRun:
    """Подставной rclone: помнит вызовы и отвечает по сценарию."""

    def __init__(self, listing=None, fail_on=None, code=0):
        self.calls = []
        self.listing = listing if listing is not None else []
        self.fail_on = fail_on          # подкоманда, на которой падаем
        self.code = code

    def __call__(self, command, capture_output=True, text=True, timeout=None):
        self.calls.append(command)
        sub = command[command.index("--log-level") + 2]
        if self.fail_on and sub == self.fail_on:
            return subprocess.CompletedProcess(command, self.code, "",
                                               "не получилось")
        if sub == "lsjson":
            return subprocess.CompletedProcess(command, 0,
                                               json.dumps(self.listing), "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture()
def copy(tmp_path):
    """Готовая «копия базы» на диске."""
    path = tmp_path / "sync_admin-20260922-120000.db"
    path.write_bytes(b"x" * 1024)
    return path


@pytest.fixture()
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_RCLONE_REMOTE", "yandex:backups")
    monkeypatch.setenv("BACKUP_RCLONE_EXE", str(tmp_path / "rclone.exe"))
    monkeypatch.setenv("BACKUP_RCLONE_CONFIG", str(tmp_path / "rclone.conf"))


def _entry(name, size):
    return {"Name": name, "Size": size}


# --------------------------------------------------------------------------
# Доехало или нет
# --------------------------------------------------------------------------

def test_a_copy_that_arrived_is_confirmed_by_reading_the_remote(copy, configured,
                                                                monkeypatch):
    """Успех объявляется только после того, как копию УВИДЕЛИ на той стороне."""
    fake = FakeRun(listing=[_entry(copy.name, 1024)])
    monkeypatch.setattr(subprocess, "run", fake)

    assert backup.upload_to_remote(copy) == ""

    subcommands = [c[c.index("--log-level") + 2] for c in fake.calls]
    assert "copyto" in subcommands
    assert "lsjson" in subcommands, "без чтения облака это не проверка"


def test_a_silent_loss_is_caught(copy, configured, monkeypatch):
    """rclone отчитался успехом, а файла там нет.

    Самый опасный исход: выгрузка «прошла», мониторинг зелёный, второй площадки
    нет. Именно от него нас не мог защитить вариант с папкой синхронизации.
    """
    monkeypatch.setattr(subprocess, "run", FakeRun(listing=[]))

    problem = backup.upload_to_remote(copy)

    assert "копии" in problem and "нет" in problem


def test_a_truncated_upload_is_caught(copy, configured, monkeypatch):
    """Размер не сошёлся — значит доехало не всё. Это не успех."""
    monkeypatch.setattr(subprocess, "run",
                        FakeRun(listing=[_entry(copy.name, 17)]))

    assert "размер не сошёлся" in backup.upload_to_remote(copy)


def test_an_unverifiable_upload_is_not_called_success(copy, configured, monkeypatch):
    """Выгрузили, а спросить не смогли.

    Сказать «копия в облаке», не увидев её там, значит вернуться ровно к тому,
    от чего уходили.
    """
    monkeypatch.setattr(subprocess, "run", FakeRun(fail_on="lsjson", code=1))

    problem = backup.upload_to_remote(copy)

    assert "проверить не удалось" in problem


# --------------------------------------------------------------------------
# Отказы, которые нельзя проглотить
# --------------------------------------------------------------------------

def test_a_missing_rclone_is_reported_plainly(copy, configured, monkeypatch):
    """Не «rclone вернул 1», а «rclone не найден»: чинится это разными вещами."""
    def boom(*a, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", boom)

    assert "не найден" in backup.upload_to_remote(copy)


def test_a_hanging_rclone_does_not_hang_the_worker(copy, configured, monkeypatch):
    """Выгрузка идёт в фоновом задании, а оно в это время не принимает заказы."""
    def hang(*a, **kw):
        raise subprocess.TimeoutExpired("rclone", backup.RCLONE_TIMEOUT)

    monkeypatch.setattr(subprocess, "run", hang)

    assert "не ответил" in backup.upload_to_remote(copy)


def test_nothing_configured_is_silent(copy, monkeypatch):
    """Облако необязательно: ненастроенное не должно выглядеть поломкой."""
    for name in ("BACKUP_RCLONE_REMOTE",):
        monkeypatch.delenv(name, raising=False)

    assert backup.upload_to_remote(copy) == ""


# --------------------------------------------------------------------------
# Уборка в облаке
# --------------------------------------------------------------------------

def test_the_remote_is_pruned_by_the_same_rule_as_the_folder(copy, configured,
                                                             monkeypatch):
    """Правило одно на все площадки.

    Разойдись они, в облаке лежало бы не то, что обещано человеку, и узнал бы он
    об этом ровно в тот день, когда полез восстанавливаться.
    """
    old = [_entry(f"sync_admin-2026{m:02d}{d:02d}-120000.db", 10)
           for m in (1, 2, 3) for d in (1, 5, 9, 14, 19, 24)]
    listing = old + [_entry(copy.name, 1024)]
    fake = FakeRun(listing=listing)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("BACKUP_MIRROR_KEEP_DAILY", "2")
    monkeypatch.setenv("BACKUP_MIRROR_KEEP_WEEKLY", "2")

    assert backup.upload_to_remote(copy) == ""

    deleted = [c[-1].split("/")[-1] for c in fake.calls
               if c[c.index("--log-level") + 2] == "deletefile"]
    expected = backup.names_to_drop([e["Name"] for e in listing],
                                    keep_daily=2, keep_weekly=2)
    assert sorted(deleted) == sorted(expected)
    assert copy.name not in deleted, "только что доставленную копию не трогаем"


def test_a_hand_placed_file_in_the_remote_is_never_deleted(copy, configured,
                                                           monkeypatch):
    """В облаке, как и в каталоге, может лежать копия, положенная руками."""
    fake = FakeRun(listing=[_entry(copy.name, 1024),
                            _entry("перед-миграцией.db", 10),
                            _entry("readme.txt", 3)])
    monkeypatch.setattr(subprocess, "run", fake)

    assert backup.upload_to_remote(copy) == ""

    deleted = [c[-1] for c in fake.calls
               if c[c.index("--log-level") + 2] == "deletefile"]
    assert deleted == []


def test_a_failed_prune_does_not_fail_a_delivered_copy(copy, configured, monkeypatch):
    """Копия доставлена и проверена — это главное.

    Лишние старые файлы занимают место, но ничего не ломают; уронить из-за них
    доставленную копию значило бы потерять важное ради второстепенного.
    """
    listing = [_entry(f"sync_admin-20250{m}01-120000.db", 10) for m in (1, 2, 3)]
    fake = FakeRun(listing=listing + [_entry(copy.name, 1024)],
                   fail_on="deletefile", code=1)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("BACKUP_MIRROR_KEEP_DAILY", "1")
    monkeypatch.setenv("BACKUP_MIRROR_KEEP_WEEKLY", "1")

    assert backup.upload_to_remote(copy) == ""


# --------------------------------------------------------------------------
# Связь с остальным бэкапом
# --------------------------------------------------------------------------

def test_the_cloud_failure_reaches_the_reader(tmp_path, configured, monkeypatch):
    """Ошибка облака складывается туда же, где её уже читают.

    У `mirror_error` есть читатель — `report._check_backup_mirror` и строка «с
    оговоркой» на «Диагностике». Заведи мы второе поле, получилась бы находка
    без читателя: ровно тот дефект, который чинили весь день.
    """
    import sqlite3

    source = tmp_path / "sync_admin.db"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE products (uid_1c TEXT)")
    con.execute("INSERT INTO products VALUES ('u1')")
    con.commit()
    con.close()

    monkeypatch.delenv("BACKUP_MIRROR_DIR", raising=False)
    monkeypatch.setattr(subprocess, "run", FakeRun(listing=[]))

    result = backup.make_backup(database_url=f"sqlite:///{source}",
                                directory=tmp_path / "backups")

    # Сам бэкап НЕ считается неудачным: локальная копия снята и прочитана.
    assert result.ok
    assert "облако" in result.mirror_error
