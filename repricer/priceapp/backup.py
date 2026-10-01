"""Копия базы — штатным механизмом SQLite на живой базе, с проверкой чтением.

Компьютер выключают на ночь, поэтому копия снимается через 10 минут после
запуска и не чаще раза в сутки. Хранится 14 последних. Ключ шифрования
(REPRICER_SECRETS_KEY) в копию не попадает и храниться рядом с ней не должен.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from priceapp import config
from priceapp.database import DATABASE_URL

MIN_GAP = timedelta(hours=20)
KEEP = 14
PREFIX = "repricer-"


def _db_path() -> Path | None:
    if not DATABASE_URL.startswith("sqlite:///"):
        return None
    return Path(DATABASE_URL[len("sqlite:///"):])


def last_backup() -> datetime | None:
    files = sorted(config.BACKUP_DIR.glob(f"{PREFIX}*.db")) if config.BACKUP_DIR.exists() else []
    if not files:
        return None
    return datetime.fromtimestamp(files[-1].stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)


def make_backup() -> str:
    """Снять копию. Возвращает текст ошибки или пустую строку."""
    src_path = _db_path()
    if src_path is None or not src_path.exists():
        return "база не SQLite-файл — копию снимают средствами СУБД"
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = config.BACKUP_DIR / f"{PREFIX}{datetime.now():%Y%m%d-%H%M%S}.db"
    tmp = dst.with_suffix(".part")
    src = sqlite3.connect(str(src_path))
    out = sqlite3.connect(str(tmp))
    try:
        src.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
        out.execute("SELECT count(*) FROM settings").fetchone()
    except sqlite3.Error as e:
        out.close()
        src.close()
        tmp.unlink(missing_ok=True)
        return f"копия не снята: {e}"
    out.close()
    src.close()
    if ok != "ok":
        tmp.rename(tmp.with_suffix(".bad"))
        return f"копия не прошла проверку: {ok}"
    tmp.rename(dst)
    for old in sorted(config.BACKUP_DIR.glob(f"{PREFIX}*.db"))[:-KEEP]:
        old.unlink(missing_ok=True)
    return ""
