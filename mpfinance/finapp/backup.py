"""Копия базы — штатным механизмом SQLite на живой базе, с проверкой чтением.

Снимается через 10 минут после запуска службы и не чаще раза в сутки, а также
накатом перед миграциями. Хранится всё за последние сутки и по копии на день за
14 дней (`names_to_drop`). Ключ шифрования (MPFIN_SECRETS_KEY) в копию не
попадает и храниться рядом с ней не должен.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from finapp import config
from finapp.database import DATABASE_URL

MIN_GAP = timedelta(hours=20)
KEEP = 14                       # календарных дней: по копии на день
KEEP_ALL_WITHIN = timedelta(hours=24)
PREFIX = "mpfinance-"


def names_to_drop(files: list[Path], now: datetime | None = None) -> list[Path]:
    """Что удалить: всё моложе суток не трогаем (копию снимает и накат, а отменять обычно
    хотят ровно то, что сделали только что); старше — по ОДНОЙ на календарный день, последнюю
    за день, за `KEEP` дней. «14 последних файлов» при частых накатах вытесняли суточные
    копии за два-три дня (так было у «Репрайсера», аудит 14.10). Файл с неразборчивым именем не трогаем никогда —
    его мог положить человек."""
    now = now or datetime.now()
    by_day: dict[str, list[tuple[datetime, Path]]] = {}
    for f in files:
        try:
            t = datetime.strptime(f.stem[len(PREFIX):], "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if now - t < KEEP_ALL_WITHIN:
            continue
        by_day.setdefault(t.strftime("%Y%m%d"), []).append((t, f))
    drop = []
    days = sorted(by_day, reverse=True)
    for i, day in enumerate(days):
        kept = sorted(by_day[day])
        drop += [f for _, f in (kept if i >= KEEP else kept[:-1])]
    return drop


def _db_path() -> Path | None:
    if not DATABASE_URL.startswith("sqlite:///"):
        return None
    return Path(DATABASE_URL[len("sqlite:///"):])


def last_backup() -> datetime | None:
    files = sorted(config.BACKUP_DIR.glob(f"{PREFIX}*.db")) if config.BACKUP_DIR.exists() else []
    if not files:
        return None
    newest = max(f.stat().st_mtime for f in files)      # не «последний по имени»: имя бывает ручным
    return datetime.fromtimestamp(newest, tz=timezone.utc).replace(tzinfo=None)


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
    for old in names_to_drop(sorted(config.BACKUP_DIR.glob(f"{PREFIX}*.db"))):
        old.unlink(missing_ok=True)
    return ""
