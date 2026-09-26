#!/usr/bin/env bash
# Резервная копия баз и скриншотов всех пользователей (у каждого своя папка
# data/users/<telegram id>/). Базы копируем через «.backup» SQLite: простое
# копирование файла во время записи может дать испорченную копию.
set -euo pipefail
DATA=${FINANCE_DATA_DIR:-/opt/finance-bot/data}
DEST=${BACKUP_DIR:-/var/backups/finance-bot}
KEEP_DAYS=${KEEP_DAYS:-30}
stamp=$(date +%Y%m%d-%H%M)
umask 077
mkdir -p "$DEST"
shopt -s nullglob
count=0
for db in "$DATA"/users/*/finance.db "$DATA"/finance.db; do
    [ -f "$db" ] || continue
    owner=$(basename "$(dirname "$db")")
    [ "$db" = "$DATA/finance.db" ] && owner=shared
    python3 - "$db" "$DEST/finance-$owner-$stamp.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
    if [ -d "$(dirname "$db")/receipts" ]; then
        tar -czf "$DEST/receipts-$owner-$stamp.tar.gz" -C "$(dirname "$db")" receipts
    fi
    count=$((count + 1))
done
find "$DEST" -type f -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $count баз(ы) в $DEST"
