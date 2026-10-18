"""Копия базы — штатным механизмом SQLite на живой базе, с проверкой чтением,
и вторая площадка — облако через rclone (`upload_to_remote`).

Снимается через 10 минут после запуска службы и не чаще раза в сутки, а также
накатом перед миграциями. Хранится всё за последние сутки и по копии на день за
14 дней (`names_to_drop`). Ключ шифрования (MPFIN_SECRETS_KEY) в копию не
попадает и храниться рядом с ней не должен.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from finapp import config
from finapp.database import DATABASE_URL

MIN_GAP = timedelta(hours=20)
KEEP = 14                       # календарных дней: по копии на день
KEEP_ALL_WITHIN = timedelta(hours=24)
PREFIX = "mpfinance-"
logger = logging.getLogger("mpfinance.backup")


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


# Недописанная (.part — служба остановлена посреди копии, сбой переименования)
# и забракованная (.bad) копии. Копия — размером с базу, а диск общий с 1С и
# Sync Admin: оставленные навсегда, они съели бы его. .bad держим сутки — на
# разбор, почему копия не прошла проверку.
LEFTOVER_KEEP = timedelta(days=1)


def sweep_leftovers() -> int:
    if not config.BACKUP_DIR.exists():
        return 0
    limit = datetime.now().timestamp() - LEFTOVER_KEEP.total_seconds()
    removed = 0
    for pattern in (f"{PREFIX}*.part", f"{PREFIX}*.bad"):
        for f in config.BACKUP_DIR.glob(pattern):
            try:
                if pattern.endswith(".part") or f.stat().st_mtime < limit:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def make_backup() -> str:
    """Снять копию. Возвращает текст ошибки или пустую строку."""
    return make_backup_ex()[0]


def make_backup_ex() -> tuple[str, Path | None]:
    """Снять копию: (текст ошибки или "", путь к проверенной копии)."""
    src_path = _db_path()
    if src_path is None or not src_path.exists():
        return "база не SQLite-файл — копию снимают средствами СУБД", None
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    sweep_leftovers()
    # Имя — с точностью до секунды, и две копии в одну секунду (накат сразу за
    # суточной, две кнопки подряд) получили бы одно имя. На Windows переименование
    # поверх существующего файла — отказ (так 18.10 встал накат fin8 на тесте), на
    # Linux — молча затёртая копия. Копия — последний рубеж, её не затираем:
    # занято — берём следующую свободную секунду, имя остаётся в формате уборки.
    stamp = datetime.now()
    dst = config.BACKUP_DIR / f"{PREFIX}{stamp:%Y%m%d-%H%M%S}.db"
    while dst.exists() or dst.with_suffix(".part").exists():
        stamp += timedelta(seconds=1)
        dst = config.BACKUP_DIR / f"{PREFIX}{stamp:%Y%m%d-%H%M%S}.db"
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
        return f"копия не снята: {e}", None
    out.close()
    src.close()
    if ok != "ok":
        tmp.replace(tmp.with_suffix(".bad"))       # прежний .bad той же секунды не нужен
        return f"копия не прошла проверку: {ok}", None
    tmp.rename(dst)
    for old in names_to_drop(sorted(config.BACKUP_DIR.glob(f"{PREFIX}*.db"))):
        old.unlink(missing_ok=True)
    return "", dst


# ---------------------------------------------------------------- вторая площадка
#
# Копии лежат на том же диске, что и база: отказ диска унёс бы и то и другое.
# Вторая площадка — облако (Selectel S3, общий бакет программ сервера, у каждой
# своя папка — см. BACKUPS.md). Устроено как у Sync Admin (`app/backup.py`):
# - после выгрузки читается список удалённой папки и сверяется РАЗМЕР —
#   копия, не увиденная на той стороне, это надежда, а не копия;
# - уборка в облаке — тем же правилом, что и локально (`names_to_drop`), и
#   только по именам наших копий: чужие файлы общего бакета ей невидимы;
# - пределы времени разные: листинг — минуты, выгрузка — часы (медленный канал
#   не «зависание»), а повисший обмен ловит сам rclone (--timeout).
# Накат зовёт `make_backup()` — БЕЗ облака: копия перед миграцией нужна на
# диске и сразу, а выгрузка шла бы минуты при остановленной службе.

RCLONE_TIMEOUT = 300
RCLONE_UPLOAD_TIMEOUT = 4 * 3600
RCLONE_CONNECT_SECONDS = 30
RCLONE_STALL_SECONDS = 300
# Сколько суток без удачной выгрузки — уже повод смотреть (суточная копия +
# запас на перезапуски и разовый сбой облака).
REMOTE_STALE = timedelta(days=3)


def _run_rclone(args: list[str], timeout: int = RCLONE_TIMEOUT) -> tuple[bool, str]:
    command = [config.RCLONE_EXE, "--config", config.RCLONE_CONFIG,
               "--contimeout", f"{RCLONE_CONNECT_SECONDS}s", "--timeout", f"{RCLONE_STALL_SECONDS}s",
               "--log-level", "ERROR"] + args
    try:
        # Кодировка ЯВНО: на боевой Windows по умолчанию cp1251, а rclone пишет
        # UTF-8 — причина отказа была бы нечитаема (урок Sync Admin 12.10).
        done = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except FileNotFoundError:
        return False, f"rclone не найден: {config.RCLONE_EXE}"
    except subprocess.TimeoutExpired:
        return False, f"rclone не ответил за {timeout} с"
    except Exception as e:                       # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().replace("\n", "; ")
        return False, f"rclone вернул {done.returncode}: {said[:300]}"
    return True, done.stdout or ""


_HTTP_CODE_RE = re.compile(r"\[\s*(\d{3})\s*-|[Ss]tatus(?:\s*[Cc]ode)?\s*:?\s*(\d{3})\b|HTTP\s+(?:error\s+)?(\d{3})\b")


def cloud_gist(said: str) -> str:
    """Что значит отказ облака — по КОДУ ответа (формы S3 и WebDAV). Пустая
    строка — код не разобран; незнакомое не выдумываем."""
    found = _HTTP_CODE_RE.search(said or "")
    code = next((int(g) for g in found.groups() if g), None) if found else None
    if code in (401, 403):
        return ("облако не пускает: ключ в rclone.conf отозван или НУЛЕВОЙ БАЛАНС Selectel "
                "(при нуле через 30 суток данные удаляются)")
    if code == 404:
        return "нет такого бакета или папки — проверьте «Путь в облаке» на опечатку"
    if code == 507:
        return "в облаке кончилось место"
    if code in (408, 410, 423, 429, 500, 502, 503, 504):
        return "сбой на стороне облака — обычно проходит сам"
    return ""


def _problem(said: str) -> str:
    gist = cloud_gist(said)
    return f"{gist} ({said[:200]})" if gist else said[:300]


def upload_to_remote(source: Path, remote: str) -> str:
    """Выгрузить проверенную копию и УВИДЕТЬ её на той стороне. "" — всё хорошо."""
    remote = remote.rstrip("/")
    ok, said = _run_rclone(["copyto", str(source), f"{remote}/{source.name}"], timeout=RCLONE_UPLOAD_TIMEOUT)
    if not ok:
        return f"облако: {_problem(said)}"
    ok, said = _run_rclone(["lsjson", remote])
    if not ok:
        return f"облако: копия отправлена, но проверить не удалось — {_problem(said)}"
    try:
        entries = json.loads(said or "[]")
    except ValueError as e:
        return f"облако: ответ rclone не разобран: {e}"
    sizes = {e.get("Name"): e.get("Size") for e in entries if isinstance(e, dict)}
    if source.name not in sizes:
        return f"облако: копии {source.name} там нет, хотя выгрузка прошла без ошибки"
    if sizes[source.name] != source.stat().st_size:
        return f"облако: размер не сошёлся — там {sizes[source.name]} байт, у нас {source.stat().st_size}"
    # Уборка — только наши копии (`names_to_drop` не трогает чужих имён), сбой
    # уборки выгрузку не роняет: копия уже доставлена и проверена.
    for old in names_to_drop([Path(n) for n in sizes if n]):
        gone, why = _run_rclone(["deletefile", f"{remote}/{old.name}"])
        if not gone:
            logger.warning("облако: не удалось удалить %s: %s", old.name, why)
    return ""


def offsite(db, source: Path) -> str:
    """Выгрузить копию на вторую площадку, если она задана, и запомнить итог.
    Возвращает текст проблемы ("" — всё хорошо или площадка не задана)."""
    from finapp import settings
    from finapp.timeutils import now_utc
    remote = (settings.get(db, settings.BACKUP_REMOTE, "") or "").strip()
    if not remote:
        return ""
    problem = upload_to_remote(source, remote)
    settings.put(db, settings.BACKUP_REMOTE_RESULT, problem or f"ok: {source.name}")
    if not problem:
        settings.put(db, settings.BACKUP_REMOTE_OK_AT, now_utc().isoformat(timespec="seconds"))
    db.commit()
    return problem


def newest_local() -> Path | None:
    files = sorted(config.BACKUP_DIR.glob(f"{PREFIX}*.db")) if config.BACKUP_DIR.exists() else []
    return max(files, key=lambda f: f.stat().st_mtime) if files else None
