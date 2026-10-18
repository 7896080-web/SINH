"""Вторая площадка копий (облако через rclone) и восстановление из копии.

rclone подменяется «облаком» в папке: copyto кладёт файл, lsjson отдаёт
список с размерами, deletefile удаляет. Проверяется то, ради чего всё
заведено: копия доехала и увидена на той стороне, уборка не трогает чужого, а
восстановление действительно подменяет базу (а не накатывает старый журнал)."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from finapp import backup, config, settings

ROOT = Path(__file__).resolve().parents[1]


class FakeCloud:
    def __init__(self, folder: Path, corrupt: bool = False):
        self.folder = folder
        self.corrupt = corrupt
        self.calls = []
        folder.mkdir(parents=True, exist_ok=True)

    def __call__(self, command, **kw):
        args = command[command.index("ERROR") + 1:]
        self.calls.append(args)
        out, code = "", 0
        if args[0] == "copyto":
            target = self.folder / args[2].split("/")[-1]
            shutil.copy(args[1], target)
            if self.corrupt:
                target.write_bytes(b"x")
        elif args[0] == "lsjson":
            out = json.dumps([{"Name": f.name, "Size": f.stat().st_size} for f in self.folder.iterdir()])
        elif args[0] == "deletefile":
            (self.folder / args[1].split("/")[-1]).unlink()
        return subprocess.CompletedProcess(command, code, stdout=out, stderr="")


def _copy(tmp_path) -> Path:
    path = tmp_path / "mpfinance-20261018-101500.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT, value TEXT)")
    conn.commit()
    conn.close()
    return path


def test_upload_is_seen_on_the_other_side_and_cleanup_spares_others(tmp_path, monkeypatch):
    """Уборка в облаке — тем же правилом, что и локально (по копии на день за
    14 дней), и только наши копии: чужие программы общего бакета и файл,
    положенный руками, ей невидимы."""
    cloud = FakeCloud(tmp_path / "cloud")
    ours = [f"mpfinance-{datetime.now() - timedelta(days=20 + i):%Y%m%d}-030000.db" for i in range(16)]
    for name in ours + ["repricer-20261001-030000.db", "ручная копия.db"]:
        (cloud.folder / name).write_bytes(b"old")
    monkeypatch.setattr(backup.subprocess, "run", cloud)
    assert backup.upload_to_remote(_copy(tmp_path), "selectel:awer-backups/mpfinance") == ""
    names = {f.name for f in cloud.folder.iterdir()}
    assert "mpfinance-20261018-101500.db" in names                       # свежая доехала
    assert {"repricer-20261001-030000.db", "ручная копия.db"} <= names    # чужое цело
    # Копия моложе суток в счёт дней не идёт (её отменяют чаще всего): из 16
    # старых дней остаются 14 последних, два самых старых уходят.
    kept = {n for n in names if n in ours}
    assert len(kept) == 14 and ours[-1] not in kept and ours[-2] not in kept


def test_upload_that_arrives_damaged_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.subprocess, "run", FakeCloud(tmp_path / "cloud", corrupt=True))
    assert "размер не сошёлся" in backup.upload_to_remote(_copy(tmp_path), "selectel:b/mpfinance")


@pytest.mark.parametrize("said,word", [
    ("Failed to copy: AccessDenied: status code: 403, request id: X", "БАЛАНС"),
    ("https response error StatusCode: 404, RequestID: Y", "опечатку"),
    ("[503 - Service Unavailable]", "проходит сам"),
    ("что-то незнакомое", ""),
])
def test_cloud_refusal_is_named_by_its_code(said, word):
    gist = backup.cloud_gist(said)
    assert (word in gist) if word else gist == ""


def test_daily_backup_goes_offsite_and_remembers_success(db, tmp_path, monkeypatch):
    from finapp.workers import jobs
    cloud = FakeCloud(tmp_path / "cloud")
    monkeypatch.setattr(backup.subprocess, "run", cloud)
    monkeypatch.setattr(jobs, "_backup_failed_at", None)
    settings.put(db, settings.BACKUP_REMOTE, "selectel:awer-backups/mpfinance")
    db.commit()
    for f in config.BACKUP_DIR.glob("mpfinance-*.db") if config.BACKUP_DIR.exists() else []:
        f.unlink()
    jobs.job_backup()
    db.expire_all()
    assert settings.get(db, settings.BACKUP_REMOTE_OK_AT)
    assert any(f.name.startswith("mpfinance-") for f in cloud.folder.iterdir())


def test_rollout_backup_never_goes_to_the_cloud(db, monkeypatch):
    """Накат зовёт make_backup(): копия перед миграцией нужна на диске сразу, а
    выгрузка шла бы при остановленной службе."""
    def no_rclone(*a, **kw):
        raise AssertionError("накат не должен звать rclone")
    monkeypatch.setattr(backup.subprocess, "run", no_rclone)
    settings.put(db, settings.BACKUP_REMOTE, "selectel:awer-backups/mpfinance")
    db.commit()
    assert backup.make_backup() == ""


def test_remote_path_is_validated_on_the_page(client, db):
    bad = client.post("/diagnostics/backup-remote", data={"remote": "--config C:/x"}, follow_redirects=True)
    assert "Путь в облаке — вида" in bad.text
    good = client.post("/diagnostics/backup-remote", data={"remote": "selectel:awer-backups/mpfinance/"},
                       follow_redirects=True)
    assert "Вторая площадка: selectel:awer-backups/mpfinance." in good.text
    assert "Вторая площадка копий" in client.get("/diagnostics").text


# ------------------------------------------------------------ восстановление

def _db(path: Path, value: str):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT, value TEXT)")
    conn.execute("DELETE FROM settings")
    conn.execute("INSERT INTO settings VALUES ('marker', ?)", (value,))
    conn.commit()
    conn.close()


def _run_restore(tmp_path, *args):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'live.db'}",
               MPFIN_BACKUP_DIR=str(tmp_path / "backups"), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "restore_db.py"), *args],
                          capture_output=True, encoding="utf-8", errors="replace", env=env, cwd=str(tmp_path))


def _marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM settings WHERE key='marker'").fetchone()[0]
    finally:
        conn.close()


def test_restore_replaces_the_base_keeps_the_old_one_and_drops_the_journal(tmp_path):
    live, copy = tmp_path / "live.db", tmp_path / "mpfinance-20261017-030000.db"
    _db(live, "сегодня")
    _db(copy, "вчера")
    (tmp_path / "live.db-wal").write_bytes("старые страницы".encode())
    done = _run_restore(tmp_path, str(copy), "--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _marker(live) == "вчера"
    assert not (tmp_path / "live.db-wal").exists()
    kept = list(tmp_path.glob("live.db.before-restore-*"))
    assert len(kept) == 1 and _marker(kept[0]) == "сегодня"


def test_broken_copy_touches_nothing(tmp_path):
    live, copy = tmp_path / "live.db", tmp_path / "mpfinance-20261017-030000.db"
    _db(live, "сегодня")
    copy.write_bytes(b"not a database")
    done = _run_restore(tmp_path, str(copy), "--yes")
    assert done.returncode == 1 and "Ничего не тронуто" in done.stdout
    assert _marker(live) == "сегодня" and not list(tmp_path.glob("live.db.before-restore-*"))
