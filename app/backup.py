"""Резервная копия базы — то единственное, чего у боевого сервера не было.

Аудит 21.09: `deploy/backup.sh` в репозитории есть, но это Linux-скрипт из
другого варианта развёртывания, а в `deploy/README_WINDOWS.md` слова «бэкап»
нет вовсе. То есть боевую базу не копировал никто.

В файле лежит всё, что не восстанавливается ниоткуда: соответствие баркодов
товарам (собиралось руками), история проведённых заказов, задания 1С с их
ответами, настройки кабинетов. 1С этого не знает, площадки — тем более. Потеря
файла означает не «откатимся на вчера», а ручную настройку каталога заново.

**Копируем штатным механизмом SQLite, а не файловой копией.** При живом WAL
часть транзакций лежит в `-wal`, и копия одного `sync_admin.db` может не
открыться вовсе или открыться без последних часов работы. `Connection.backup()`
делает согласованный снимок НА ЖИВОЙ БАЗЕ, не останавливая службы: он читает
страницы порциями и повторяет те, что изменились по ходу. Именно поэтому здесь
нет ни остановки служб, ни предупреждений про «делайте ночью».

Копия проверяется сразу после создания (`PRAGMA integrity_check` плюс чтение
таблицы товаров). Непроверенная копия — это не копия, а надежда: о том, что она
битая, узнают в тот единственный день, когда она нужна.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.timeutils import now_utc

logger = logging.getLogger("sync_worker")

# Куда складывать. Отдельный каталог, а не рядом с базой: копия рядом с
# оригиналом не переживает ровно того случая, ради которого делается, —
# потери диска или каталога целиком.
BACKUP_DIR_ENV = "BACKUP_DIR"
DEFAULT_BACKUP_DIR = r"C:\sync_admin\backups"

# Сколько храним. Ежедневных две недели — этого хватает, чтобы заметить порчу
# данных, которую видно не сразу (перепутанный мэппинг, неверный импорт).
# Плюс по одной копии на неделю за два месяца: они ловят то, что заметили
# поздно, и стоят копейки против цены «восстанавливать нечего».
KEEP_DAILY = 14
KEEP_WEEKLY = 8

NAME_PREFIX = "sync_admin-"
NAME_RE = re.compile(r"^sync_admin-(\d{8})-(\d{6})\.db$")


@dataclass
class BackupResult:
    path: str
    size_bytes: int
    checked: bool
    removed: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.checked and not self.error


def backup_dir() -> Path:
    return Path(os.environ.get(BACKUP_DIR_ENV) or DEFAULT_BACKUP_DIR)


def database_path(database_url: str | None = None) -> Path | None:
    """Путь к файлу SQLite из DATABASE_URL. None — база не файловая.

    Бэкапить умеем только SQLite: у PostgreSQL свой механизм, и делать вид,
    что мы его прикрыли, опаснее, чем честно ничего не делать.
    """
    url = database_url if database_url is not None else os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite"):
        return None
    tail = url.split("///", 1)[-1]
    if not tail or tail == ":memory:":
        return None
    return Path(tail)


def _verify(path: Path) -> str:
    """Проверка копии. Пустая строка — всё в порядке, иначе причина.

    `integrity_check` проверяет структуру, а чтение товаров — что данные на
    месте: битый, но структурно целый файл бывает, и он выглядит нормально до
    первого обращения.
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            status = con.execute("PRAGMA integrity_check").fetchone()
            if not status or status[0] != "ok":
                return f"integrity_check: {status[0] if status else 'нет ответа'}"
            con.execute("SELECT COUNT(*) FROM products").fetchone()
        finally:
            con.close()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return ""


def _parse_moment(name: str) -> datetime | None:
    match = NAME_RE.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def prune(directory: Path, keep_daily: int = KEEP_DAILY,
          keep_weekly: int = KEEP_WEEKLY) -> int:
    """Удалить лишние копии. Возвращает сколько удалено.

    Правило двухслойное намеренно. Последние `keep_daily` копий — по одной на
    день, чтобы вернуться на любой из недавних дней. Дальше — по одной на
    календарную неделю: порчу данных замечают и через месяц, а хранить тридцать
    ежедневных копий ради этого незачем.

    Файл с неразборчивым именем не трогаем никогда: в каталоге бэкапов может
    лежать копия, положенная человеком руками, и удалить её значит сделать ровно
    то, от чего этот модуль защищает.
    """
    files = []
    for path in directory.glob(f"{NAME_PREFIX}*.db"):
        moment = _parse_moment(path.name)
        if moment is not None:
            files.append((moment, path))
    files.sort(reverse=True)

    keep: set[Path] = {path for _, path in files[:keep_daily]}
    weeks_seen: set[tuple] = set()
    for moment, path in files[keep_daily:]:
        week = moment.isocalendar()[:2]
        if week not in weeks_seen and len(weeks_seen) < keep_weekly:
            weeks_seen.add(week)
            keep.add(path)

    removed = 0
    for _, path in files:
        if path not in keep:
            try:
                path.unlink()
                removed += 1
            except OSError as e:
                logger.warning("бэкап: не удалось удалить %s: %s", path, e)
    return removed


def make_backup(database_url: str | None = None,
                directory: Path | None = None) -> BackupResult:
    """Снять копию, проверить её и подчистить старые.

    Битая копия НЕ удаляется и остаётся на диске: она может пригодиться для
    разбора, а главное — её присутствие вместе с ошибкой в журнале честнее, чем
    пустой каталог, по которому не понять, была попытка или нет.
    """
    source = database_path(database_url)
    if source is None:
        return BackupResult(path="", size_bytes=0, checked=False,
                            error="база не SQLite — копию снимать нечем")
    if not source.exists():
        return BackupResult(path="", size_bytes=0, checked=False,
                            error=f"файла базы нет: {source}")

    directory = directory or backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{NAME_PREFIX}{now_utc():%Y%m%d-%H%M%S}.db"

    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(target)
            try:
                # Порциями по тысяче страниц: длинная блокировка на живой базе
                # нам не нужна — в неё в это же время пишут воркер и веб.
                src.backup(dst, pages=1000)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as e:
        return BackupResult(path=str(target), size_bytes=0, checked=False,
                            error=f"копирование не удалось: {type(e).__name__}: {e}")

    problem = _verify(target)
    size = target.stat().st_size if target.exists() else 0
    if problem:
        return BackupResult(path=str(target), size_bytes=size, checked=False,
                            error=f"копия не прошла проверку: {problem}")

    removed = prune(directory)
    return BackupResult(path=str(target), size_bytes=size, checked=True, removed=removed)


def last_backup(directory: Path | None = None) -> tuple[datetime | None, int]:
    """(время последней копии, сколько копий всего). Для /health и отчёта."""
    directory = directory or backup_dir()
    if not directory.exists():
        return None, 0
    moments = [m for m in (_parse_moment(p.name)
                           for p in directory.glob(f"{NAME_PREFIX}*.db"))
               if m is not None]
    return (max(moments) if moments else None), len(moments)
