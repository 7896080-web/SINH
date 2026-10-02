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
        # Скриншоты — зеркалом: докопируются только новые (а не весь архив каждый день).
        mkdir -p "$DEST/receipts-$owner"
        cp -ru "$(dirname "$db")/receipts/." "$DEST/receipts-$owner/" \
            || echo "предупреждение: не все скриншоты $owner скопированы" >&2
    fi
    count=$((count + 1))
done
# Только свои файлы: копии баз и архивы скриншотов прежней версии.
find "$DEST" -maxdepth 1 -type f \( -name 'finance-*.db' -o -name 'receipts-*.tar.gz' \) \
    -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $count баз(ы) в $DEST"
