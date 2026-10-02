"""Резервная копия баз и скриншотов всех пользователей (для Windows; на
Linux то же делает deploy/finance-bot-backup.sh).

    python -m finance.backup --data C:\\FinanceBot\\data --dest C:\\FinanceBot\\backups

- Базы копируются через backup() SQLite (простое копирование файла во время
  записи может дать испорченную копию) и проверяются integrity_check.
  Копии старше --keep-days удаляются — до того, как делать новые: если диск
  заполнен, место освобождается, а не кончается окончательно.
- Скриншоты — не архивом каждый день (это был бы весь архив заново, каждый
  день), а зеркалом: в backups\\receipts-<id>\\ докопируются только новые файлы.
- Рядом кладётся копия .env (токены, список пользователей) — для восстановления.

Восстановление — см. УСТАНОВКА-WINDOWS.md, раздел «Восстановление из бэкапа».
"""

import argparse
import glob
import os
import shutil
import sqlite3
import sys
import time


def _prune(dest: str, keep_days: int):
    limit = time.time() - keep_days * 86400
    for name in os.listdir(dest):
        path = os.path.join(dest, name)
        # finance-*.db — копии баз; receipts-*.zip — архивы скриншотов прежней версии.
        dated = name.startswith("finance-") or (name.startswith("receipts-") and name.endswith(".zip"))
        if os.path.isfile(path) and dated and os.path.getmtime(path) < limit:
            os.remove(path)


def _mirror(src: str, dst: str) -> int:
    """Докопировать в dst новые и изменённые файлы из src. Сколько скопировано."""
    copied = 0
    for root, _, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_dir = os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            source, target = os.path.join(root, name), os.path.join(target_dir, name)
            try:
                st = os.stat(source)
            except FileNotFoundError:
                continue  # файл как раз перенесли/удалили — возьмём в следующий раз
            if os.path.exists(target) and os.path.getsize(target) == st.st_size:
                continue
            tmp = target + ".part"
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
            copied += 1
    return copied


def _copy_db(db: str, target: str):
    tmp = target + ".part"
    src = sqlite3.connect(db)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"копия {db} не прошла проверку: {ok}")
    finally:
        dst.close()
        src.close()
    os.replace(tmp, target)


def backup(data: str, dest: str, keep_days: int = 30, stamp: str | None = None,
           env_file: str | None = None) -> int:
    stamp = stamp or time.strftime("%Y%m%d-%H%M")
    os.makedirs(dest, exist_ok=True)
    dbs = sorted(glob.glob(os.path.join(data, "users", "*", "finance.db")))
    legacy = os.path.join(data, "finance.db")
    if os.path.exists(legacy):
        dbs.append(legacy)
    if not dbs:
        # Баз ещё нет (никто не писал боту) или не та папка: копировать нечего,
        # а старые копии не трогаем — возможно, только они и остались.
        return 0
    _prune(dest, keep_days)  # сначала место, потом новые копии
    for leftover in glob.glob(os.path.join(dest, "*.part")):
        os.remove(leftover)  # недоделанное от прошлого неудачного запуска
    count, errors = 0, []
    for db in dbs:
        folder = os.path.dirname(db)
        owner = "shared" if db == legacy else os.path.basename(folder)
        try:  # сбой у одного пользователя не оставляет без копии остальных
            _copy_db(db, os.path.join(dest, f"finance-{owner}-{stamp}.db"))
            receipts = os.path.join(folder, "receipts")
            if os.path.isdir(receipts):
                _mirror(receipts, os.path.join(dest, f"receipts-{owner}"))
            count += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{owner}: {exc}")
    if env_file and os.path.exists(env_file):
        shutil.copy2(env_file, os.path.join(dest, "env-latest.txt"))
    if errors:
        raise RuntimeError("бэкап сделан не для всех: " + "; ".join(errors))
    return count


def main(argv=None):
    p = argparse.ArgumentParser(description="Бэкап баз финансового помощника")
    p.add_argument("--data", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--keep-days", type=int, default=30)
    p.add_argument("--env", help="файл настроек .env — положить его копию рядом")
    args = p.parse_args(argv)
    count = backup(args.data, args.dest, args.keep_days, env_file=args.env)
    free = shutil.disk_usage(args.dest).free // 2**20
    print(f"backup ok: {count} баз(ы) в {args.dest}; свободно на диске {free} МБ")


if __name__ == "__main__":
    sys.exit(main())
