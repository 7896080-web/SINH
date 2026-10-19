"""Обслуживание на сервере: резервная копия базы и сводка состояния (scripts/status.ps1, scripts/cloud_copy.ps1).

Копия живой SQLite — только снимком через Connection.backup() с PRAGMA integrity_check (BACKUPS.md: файловая копия
работающей базы бывает битой). Имя `awer-ГГГГММДД-ЧЧММ.db` — по нему задача копирования в облако выбирает файлы.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from . import health
from .config import Settings
from .db import DB

PREFIX = "awer-"


def backup(db_path: Path, dest: Path, keep_days: int = 30, now: datetime | None = None) -> Path:
    """Снимок базы в dest/awer-ГГГГММДД-ЧЧММ.db; снимки старше keep_days удаляются. Битый снимок — исключение,
    файл не остаётся."""
    now = now or datetime.now()
    if not db_path.exists():
        raise FileNotFoundError(f"базы нет: {db_path}")
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{PREFIX}{now:%Y%m%d-%H%M}.db"
    part = out.with_suffix(".part")
    part.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(part)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if check != "ok":
        part.unlink(missing_ok=True)
        raise RuntimeError(f"снимок не прошёл integrity_check: {check}")
    os.replace(part, out)
    for old in dest.glob(f"{PREFIX}*.db"):
        if old != out and datetime.fromtimestamp(old.stat().st_mtime) < now - timedelta(days=keep_days):
            old.unlink()
    return out


def doctor(db: DB, cfg: Settings, backups: Path | None = None, today: date | None = None) -> list[tuple[bool, str]]:
    """[(в порядке ли, строка)] — то, что владельцу или разработчику нужно увидеть первым."""
    today = today or date.today()
    out: list[tuple[bool, str]] = []
    sellers = db.q("SELECT role, max_user_id FROM sellers WHERE active=1 AND role<>'system'")
    admins = sum(1 for s in sellers if s["role"] == "admin")
    linked = sum(1 for s in sellers if s["max_user_id"])
    out.append((bool(sellers) and admins > 0,
                f"продавцов {len(sellers)}, из них старших {admins}, подключены к боту {linked}"
                + ("" if admins else " — отметьте старших: «Продавцы» → роль")))
    last = {r["store"]: r["d"] for r in db.q(
        "SELECT store, MAX(day) d FROM imported_days WHERE source<>'closed' GROUP BY store")}
    for st in cfg.stores:
        d = last.get(st)
        fresh = d is not None and date.fromisoformat(d) >= today - timedelta(days=2)
        out.append((fresh, f"1С, {st}: последний день в базе — {date.fromisoformat(d):%d.%m.%Y}" if d
                    else f"1С, {st}: данных ещё нет — выгрузите историю (docs/УСТАНОВКА.md)"))
    first = db.one("SELECT MIN(day) d FROM imported_days")["d"]
    if first:
        gaps = db.data_gaps(today - timedelta(days=2))
        out.append((not gaps, "дней без данных и без отметки «закрыто»: " + (", ".join(
            date.fromisoformat(d).strftime("%d.%m.%Y") for d in gaps[:10]) + (" …" if len(gaps) > 10 else "")
            + " — «График работы» → «Закрытые дни»" if gaps else "нет")))
    inbox = [p for p in cfg.onec.inbox.glob("*") if p.is_file() and not p.name.startswith(".")]
    out.append((len(inbox) == 0, f"файлов в {cfg.onec.inbox} ждут загрузки: {len(inbox)}"))
    month = today.strftime("%Y-%m")
    frozen = db.one("SELECT COUNT(*) n, MAX(frozen_at) t FROM plans WHERE month=?", month)
    out.append((frozen["n"] > 0, f"план на {month} зафиксирован ({frozen['n']} продавцов)" if frozen["n"]
                else f"план на {month} ещё не зафиксирован — «Настройки месяца» → «Зафиксировать план»"))
    out.append((bool(os.environ.get("ADMIN_PASSWORD")), "пароль админки задан" if os.environ.get("ADMIN_PASSWORD")
                else "ADMIN_PASSWORD в .env пуст — админка не пустит"))
    if not os.environ.get("MAX_BOT_TOKEN"):
        out.append((False, "токен бота «План» не задан — админка → «Бот MAX»"))
    else:
        out.append(health.bot_state(db, "plan"))
        if os.environ.get("MAX_TRAINING_BOT_TOKEN"):
            out.append(health.bot_state(db, "training"))
        out.append(health.daily_state(db, cfg))
    if backups is not None:
        snaps = sorted(backups.glob(f"{PREFIX}*.db"), key=lambda p: p.stat().st_mtime)
        if snaps:
            age = datetime.now() - datetime.fromtimestamp(snaps[-1].stat().st_mtime)
            out.append((age < timedelta(hours=36), f"последняя копия базы: {snaps[-1].name} "
                        f"({int(age.total_seconds() // 3600)} ч назад), всего копий {len(snaps)}"))
        else:
            out.append((False, f"копий базы в {backups} ещё нет"))
    return out
