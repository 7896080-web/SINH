"""Восстановить базу «Финансов площадок» из копии.

    python scripts\\restore_db.py --list
    python scripts\\restore_db.py C:\\mpfinance\\backups\\mpfinance-20261018-101500.db
    python scripts\\restore_db.py --from-cloud mpfinance-20261018-101500.db

Порядок (все шаги обязательны — урок Sync Admin, где восстановление из четырёх
команд молча не восстанавливало ничего):
1. Служба `mpfinance` должна быть ОСТАНОВЛЕНА (`nssm stop mpfinance`): живая
   служба допишет в базу поверх подложенной копии.
2. Копия проверяется (`integrity_check`, чтение настроек) ДО того, как
   что-либо тронуто.
3. Текущая база сохраняется штатным механизмом SQLite (с содержимым журнала)
   рядом, под именем `.before-restore-<время>` — откатить восстановление.
4. Спутники `-wal`/`-shm` текущей базы убираются: иначе SQLite при первом
   открытии накатил бы ИХ страницы поверх подложенной копии, и восстановление
   не восстановило бы ничего, при `integrity_check` = ok.
5. Копия кладётся на место базы и проверяется чтением ещё раз.

Ключ `MPFIN_SECRETS_KEY` в `.env` должен быть тем же, что при снятии копии:
иначе токены площадок и пароль почты в восстановленной базе не расшифруются
(их придётся вписать заново на «API-ключах»).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Консоль боевого сервера — cp1251: чужой символ в имени файла не должен
# оборвать вывод на полуслове.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:                                # noqa: BLE001
    pass

import finapp  # noqa: E402,F401 — читает .env
from finapp import backup, config  # noqa: E402

SERVICE = "mpfinance"


def say(text: str) -> None:
    print(text, flush=True)


def service_running() -> bool | None:
    """True — служба работает, False — стоит, None — не Windows/нет sc."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["sc", "query", SERVICE], capture_output=True, encoding="cp866",
                             errors="replace", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # Имена состояний sc печатает по-английски и в русской Windows.
    return "RUNNING" in out or "START_PENDING" in out or "STOP_PENDING" in out


def check_copy(path: Path) -> str:
    """Пусто — копия читается; иначе причина."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.execute("SELECT count(*) FROM settings").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return f"копия не читается: {e}"
    return "" if ok == "ok" else f"копия не прошла проверку: {ok}"


def list_copies() -> int:
    local = sorted(config.BACKUP_DIR.glob(f"{backup.PREFIX}*.db")) if config.BACKUP_DIR.exists() else []
    say(f"Копии на диске ({config.BACKUP_DIR}):")
    for f in local:
        say(f"  {f.name}  {f.stat().st_size // 1024} КБ")
    if not local:
        say("  нет")
    from finapp import settings
    from finapp.database import SessionLocal
    try:
        db = SessionLocal()
        remote = (settings.get(db, settings.BACKUP_REMOTE, "") or "").strip()
        db.close()
    except Exception:                            # noqa: BLE001 — база может быть сломана
        remote = ""
    if remote:
        ok, said = backup._run_rclone(["lsf", remote])
        say(f"Копии в облаке ({remote}):")
        say("\n".join(f"  {n}" for n in said.splitlines() if n.startswith(backup.PREFIX)) if ok else f"  {said}")
    return 0


def fetch_from_cloud(name: str) -> Path | None:
    from finapp import settings
    from finapp.database import SessionLocal
    db = SessionLocal()
    remote = (settings.get(db, settings.BACKUP_REMOTE, "") or "").strip().rstrip("/")
    db.close()
    if not remote:
        say("[X] Путь в облаке не задан («Диагностика» -> «Вторая площадка копий»).")
        return None
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = config.BACKUP_DIR / name
    say(f"[*] Скачиваю {remote}/{name} ...")
    ok, said = backup._run_rclone(["copyto", f"{remote}/{name}", str(target)],
                                  timeout=backup.RCLONE_UPLOAD_TIMEOUT)
    if not ok:
        say(f"[X] Не скачано: {said}")
        return None
    return target


def restore(copy: Path, yes: bool) -> int:
    db_path = backup._db_path()
    if db_path is None:
        say("[X] База не SQLite-файл — восстановление средствами СУБД.")
        return 1
    if not copy.exists():
        say(f"[X] Нет файла {copy}")
        return 1
    running = service_running()
    if running:
        say(f"[X] Служба {SERVICE} работает. Сначала: nssm stop {SERVICE}")
        return 1
    if running is None:
        say("[!] Состояние службы не проверено (не Windows) — убедитесь, что программа остановлена.")
    problem = check_copy(copy)
    if problem:
        say(f"[X] {problem}. Ничего не тронуто.")
        return 1
    say(f"[OK] Копия {copy.name} читается.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = db_path.with_name(f"{db_path.name}.before-restore-{stamp}")
    if not yes:
        answer = input(f"Заменить базу {db_path} копией {copy.name}? Текущая сохранится как {keep.name}. "
                       f"Введите ДА: ").strip()
        if answer.upper() != "ДА":
            say("Отменено. Ничего не тронуто.")
            return 1
    if db_path.exists():
        src = sqlite3.connect(str(db_path))
        out = sqlite3.connect(str(keep))
        try:
            src.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")
        finally:
            out.close()
            src.close()
        say(f"[OK] Текущая база сохранена: {keep}")
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            side.unlink()
            say(f"[OK] Убран спутник {side.name} (иначе его страницы легли бы поверх копии)")
    tmp = db_path.with_name(db_path.name + ".restore-part")
    shutil.copy2(copy, tmp)
    os.replace(tmp, db_path)
    problem = check_copy(db_path)
    if problem:
        say(f"[X] После замены база не читается: {problem}. Верните прежнюю: {keep}")
        return 1
    say(f"[OK] База восстановлена из {copy.name} и читается.")
    say(f"Дальше: nssm start {SERVICE} — и проверьте «Диагностику».")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Восстановить базу «Финансов площадок» из копии")
    ap.add_argument("copy", nargs="?", help="путь к копии mpfinance-*.db")
    ap.add_argument("--from-cloud", metavar="ИМЯ", help="скачать копию с этим именем из облака")
    ap.add_argument("--list", action="store_true", help="показать копии на диске и в облаке")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    a = ap.parse_args()
    if a.list:
        return list_copies()
    if a.from_cloud:
        copy = fetch_from_cloud(a.from_cloud)
        if copy is None:
            return 1
    elif a.copy:
        copy = Path(a.copy)
    else:
        ap.print_help()
        return 1
    return restore(copy, a.yes)


if __name__ == "__main__":
    sys.exit(main())
