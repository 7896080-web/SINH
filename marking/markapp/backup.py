"""Резервная копия базы программы (ТЗ, разд. 11).

Механизм перенесён из sync_admin (`app/backup.py`), где он проверен на бою;
комментарии там подробнее — здесь коротко, почему каждое правило стоит:

- копия снимается ШТАТНЫМ `Connection.backup()` на живой базе, ОДНИМ шагом:
  порциями при чужой записи он начинает копирование заново и может не
  завершиться никогда;
- копия сводится в один файл (`journal_mode=DELETE`) — иначе рядом копятся
  `-wal`/`-shm`, которых уборка не видит;
- копия проверяется `integrity_check` и чтением: непроверенная копия — надежда;
- сорвавшаяся попытка уходит под `.bad`, а не остаётся под именем копии:
  иначе она проходила бы за полноценную;
- хранение — по одной на местный календарный день плюс недельные, а всё, что
  моложе суток, не удаляется: копию до ошибки человека нельзя вытеснить копией
  после неё;
- облако — СВОЙ rclone и СВОЙ конфиг (`C:\\marking\\tools\\rclone.exe`,
  `C:\\marking\\rclone.conf`): rclone переписывает конфиг при обновлении
  токена, и общий с sync_admin файл две программы могли бы испортить разом;
- после выгрузки в облако читается список папки и сверяется размер.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from markapp import config
from markapp.timeutils import local_date_of, now_utc

logger = logging.getLogger(__name__)

NAME_PREFIX = "marking-"
NAME_RE = re.compile(r"^marking-(\d{8})-(\d{6})\.db$")
KEEP_DAILY = 14
KEEP_WEEKLY = 8
KEEP_ALL_WITHIN_HOURS = 24
REMOTE_KEEP_DAILY = 30
REMOTE_KEEP_WEEKLY = 12
RCLONE_TIMEOUT = 900
# Внеочередная копия (после получения кодов) не глушится этим пределом; он
# только для задания по расписанию, чтобы перезапуск воркера не плодил копии.
MIN_GAP = timedelta(hours=20)

RCLONE_EXE = os.environ.get("MARKING_RCLONE_EXE", r"C:\marking\tools\rclone.exe")
RCLONE_CONFIG = os.environ.get("MARKING_RCLONE_CONFIG", r"C:\marking\rclone.conf")
# Пусто — облако не настроено (свежая установка): это не ошибка, но видно на
# «Диагностике».
RCLONE_REMOTE = os.environ.get("MARKING_RCLONE_REMOTE", "")


@dataclass
class BackupResult:
    path: str
    size_bytes: int
    checked: bool
    error: str = ""
    remote_error: str = ""
    removed: int = 0


def database_path(url: str | None = None) -> Path | None:
    url = url if url is not None else os.environ.get("DATABASE_URL", "sqlite:///./marking.db")
    if not url.startswith("sqlite"):
        return None
    tail = url.split("///", 1)[-1]
    return None if not tail or tail == ":memory:" else Path(tail)


def _remove_companions(target: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            target.with_name(target.name + suffix).unlink()
        except OSError:
            pass


def _set_aside(target: Path) -> None:
    if target.exists():
        bad = target.with_suffix(target.suffix + ".bad")
        try:
            if bad.exists():
                bad.unlink()
            target.replace(bad)
        except OSError:
            pass
    _remove_companions(target)


def _collapse_journal(path: Path) -> None:
    try:
        con = sqlite3.connect(path)
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.commit()
        finally:
            con.close()
    except Exception as e:  # копия уже снята и целостна — спутники второстепенны
        logger.warning("бэкап: журнал копии не свёрнут (%s): %s", path, e)
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except OSError:
            pass


def _verify(path: Path) -> str:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            status = con.execute("PRAGMA integrity_check").fetchone()
            if not status or status[0] != "ok":
                return f"integrity_check: {status[0] if status else 'нет ответа'}"
            con.execute("SELECT COUNT(*) FROM supplies").fetchone()
        finally:
            con.close()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return ""


def _parse_moment(name: str) -> datetime | None:
    m = NAME_RE.match(name)
    if m is None:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def names_to_drop(names: list[str], keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY,
                  now: datetime | None = None) -> list[str]:
    """Одно правило на папку и облако. Неразборчивое имя не удаляется никогда:
    это может быть копия, положенная человеком руками."""
    now = now or now_utc()
    parsed = sorted(((m, n) for n in names if (m := _parse_moment(n)) is not None), reverse=True)
    keep: set[str] = {n for m, n in parsed if m >= now - timedelta(hours=KEEP_ALL_WITHIN_HOURS)}
    days: set = set()
    rest = []
    for m, n in parsed:
        day = local_date_of(m)     # местный день: копия до 3 ночи — это уже новые сутки
        if day not in days and len(days) < keep_daily:
            days.add(day)
            keep.add(n)
        else:
            rest.append((m, n))
    weeks: set = set()
    for m, n in rest:
        if n in keep:
            continue   # свежая копия не занимает недельный слот
        week = local_date_of(m).isocalendar()[:2]
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.add(week)
            keep.add(n)
    return [n for _, n in parsed if n not in keep]


def prune(directory: Path) -> int:
    drop = set(names_to_drop([p.name for p in directory.glob(f"{NAME_PREFIX}*.db")]))
    removed = 0
    for name in drop:
        path = directory / name
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            logger.warning("бэкап: не удалось удалить %s: %s", path, e)
            continue
        _remove_companions(path)
    return removed


def _run_rclone(args: list[str]) -> tuple[bool, str]:
    command = [RCLONE_EXE, "--config", RCLONE_CONFIG, "--log-level", "ERROR"] + args
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=RCLONE_TIMEOUT)
    except FileNotFoundError:
        return False, f"rclone не найден: {RCLONE_EXE}"
    except subprocess.TimeoutExpired:
        return False, f"rclone не ответил за {RCLONE_TIMEOUT} с"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().replace("\n", "; ")
        return False, f"rclone вернул {done.returncode}: {said[:300]}"
    return True, done.stdout or ""


def upload_to_remote(source: Path) -> str:
    """Выгрузить и СРАЗУ проверить, что копия там есть и размер тот же."""
    remote = RCLONE_REMOTE.rstrip("/")
    if not remote:
        return ""
    ok, said = _run_rclone(["copyto", str(source), f"{remote}/{source.name}"])
    if not ok:
        return f"облако: {said}"
    ok, said = _run_rclone(["lsjson", remote])
    if not ok:
        return f"облако: копия отправлена, но проверить не удалось — {said}"
    try:
        entries = json.loads(said or "[]")
    except ValueError as e:
        return f"облако: ответ rclone не разобран — {e}"
    sizes = {e.get("Name"): e.get("Size") for e in entries if isinstance(e, dict)}
    if source.name not in sizes:
        return f"облако: копии {source.name} там нет, хотя выгрузка прошла без ошибки"
    if sizes[source.name] != source.stat().st_size:
        return f"облако: размер не сошёлся — там {sizes[source.name]}, у нас {source.stat().st_size}"
    for name in names_to_drop(list(sizes), REMOTE_KEEP_DAILY, REMOTE_KEEP_WEEKLY):
        gone, why = _run_rclone(["deletefile", f"{remote}/{name}"])
        if not gone:
            logger.warning("облако: не удалось удалить %s: %s", name, why)
    return ""


def make_backup(url: str | None = None, directory: Path | None = None) -> BackupResult:
    source = database_path(url)
    if source is None:
        return BackupResult("", 0, False, "база не SQLite — копию снимать нечем")
    if not source.exists():
        return BackupResult("", 0, False, f"файла базы нет: {source}")
    directory = Path(directory or config.BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{NAME_PREFIX}{now_utc():%Y%m%d-%H%M%S}.db"
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as e:
        _set_aside(target)
        return BackupResult(str(target), 0, False, f"копирование не удалось: {type(e).__name__}: {e}")
    _collapse_journal(target)
    problem = _verify(target)
    size = target.stat().st_size if target.exists() else 0
    if problem:
        _set_aside(target)
        return BackupResult(str(target), size, False, f"копия не прошла проверку: {problem}")
    removed = prune(directory)
    return BackupResult(str(target), size, True, removed=removed,
                        remote_error=upload_to_remote(target))


def last_backup(directory: Path | None = None) -> datetime | None:
    directory = Path(directory or config.BACKUP_DIR)
    if not directory.exists():
        return None
    moments = [m for m in (_parse_moment(p.name) for p in directory.glob(f"{NAME_PREFIX}*.db")) if m]
    return max(moments) if moments else None
