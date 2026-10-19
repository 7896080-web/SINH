"""Установка на сервер: .env из .env.example, снимок базы для копий в облако, сводка состояния (scripts/*.ps1)."""
import os
import sqlite3
import time
from datetime import date, datetime

import pytest

from awer_sales_bot import cli, ops
from awer_sales_bot.config import _load_dotenv, env_value
from awer_sales_bot.planning import service
from test_flow import env  # noqa: F401


@pytest.mark.parametrize("raw,value", [
    ("            # токен бота «План»", ""),  # строка из .env.example как есть — пусто, а не текст комментария
    ("secret-1   # пароль админки", "secret-1"),
    ("abc#def", "abc#def"),  # решётка без пробела перед ней — часть значения
    ('"пароль с #решёткой"', "пароль с #решёткой"),
    ("'x'", "x"),
    ("", ""),
])
def test_env_value(raw, value):
    assert env_value(raw) == value


def test_dotenv_with_bom_and_comments(tmp_path, monkeypatch):
    for k in ("AWER_T1", "AWER_T2"):
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text("AWER_T1=one   # комментарий\nAWER_T2=            # пусто\n", encoding="utf-8-sig")
    _load_dotenv(p)
    assert os.environ["AWER_T1"] == "one" and os.environ["AWER_T2"] == ""


def test_backup_snapshot_and_retention(env, tmp_path):  # noqa: F811
    cfg, db = env
    dest = tmp_path / "backups"
    old = dest / "awer-20260901-0300.db"
    dest.mkdir()
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    keep = dest / "awer-20261010-0300.db"
    keep.write_bytes(b"y")
    out = ops.backup(cfg.db_path, dest, keep_days=30, now=datetime(2026, 10, 19, 3, 40))
    assert out.name == "awer-20261019-0340.db" and not old.exists() and keep.exists()
    con = sqlite3.connect(out)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM monthly_history").fetchone()[0] > 0  # данные на месте
    con.close()
    assert not list(dest.glob("*.part"))
    with pytest.raises(FileNotFoundError):
        ops.backup(tmp_path / "нет.db", dest)


def test_backup_and_doctor_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    (tmp_path / "config.yaml").write_text("db_path: data/awer.db\n", encoding="utf-8")
    (tmp_path / "data" / "inbox").mkdir(parents=True)
    cli.main(["init-db"])
    cli.main(["backup", "--dest", "backups", "--log", "logs/backup.log"])
    assert len(list((tmp_path / "backups").glob("awer-*.db"))) == 1
    assert " ok awer-" in (tmp_path / "logs" / "backup.log").read_text(encoding="utf-8")
    capsys.readouterr()
    cli.main(["doctor"])
    text = capsys.readouterr().out
    assert "данных ещё нет" in text and "токен бота «План» не задан" in text and "последняя копия базы" in text


def test_doctor_after_history(env, tmp_path):  # noqa: F811
    cfg, db = env
    service.freeze(db, cfg, date.today().strftime("%Y-%m"))
    lines = ops.doctor(db, cfg, tmp_path, today=date.today())
    assert any(good and text.startswith("план на") for good, text in lines)
    assert (False, f"копий базы в {tmp_path} ещё нет") in lines  # путь C:\... — не делить строку по «:»
