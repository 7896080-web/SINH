"""Резервная копия баз и скриншотов всех пользователей (для Windows; на
Linux то же делает deploy/finance-bot-backup.sh).

    python -m finance.backup --data C:\\FinanceBot\\data --dest C:\\FinanceBot\\backups

Базы копируются через backup() SQLite: простое копирование файла во время
записи может дать испорченную копию. Копии старше --keep-days удаляются.
"""

import argparse
import glob
import os
import shutil
import sqlite3
import sys
import time


def backup(data: str, dest: str, keep_days: int = 30, stamp: str | None = None) -> int:
    stamp = stamp or time.strftime("%Y%m%d-%H%M")
    os.makedirs(dest, exist_ok=True)
    count = 0
    dbs = sorted(glob.glob(os.path.join(data, "users", "*", "finance.db")))
    legacy = os.path.join(data, "finance.db")
    if os.path.exists(legacy):
        dbs.append(legacy)
    for db in dbs:
        folder = os.path.dirname(db)
        owner = "shared" if db == legacy else os.path.basename(folder)
        src = sqlite3.connect(db)
        dst = sqlite3.connect(os.path.join(dest, f"finance-{owner}-{stamp}.db"))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        receipts = os.path.join(folder, "receipts")
        if os.path.isdir(receipts):
            shutil.make_archive(os.path.join(dest, f"receipts-{owner}-{stamp}"), "zip",
                                root_dir=folder, base_dir="receipts")
        count += 1
    limit = time.time() - keep_days * 86400
    for name in os.listdir(dest):
        path = os.path.join(dest, name)
        if os.path.isfile(path) and os.path.getmtime(path) < limit:
            os.remove(path)
    return count


def main(argv=None):
    p = argparse.ArgumentParser(description="Бэкап баз финансового помощника")
    p.add_argument("--data", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--keep-days", type=int, default=30)
    args = p.parse_args(argv)
    count = backup(args.data, args.dest, args.keep_days)
    print(f"backup ok: {count} баз(ы) в {args.dest}")


if __name__ == "__main__":
    sys.exit(main())
