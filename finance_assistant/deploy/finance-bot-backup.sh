#!/usr/bin/env bash
# Резервная копия базы и скриншотов. Базу копируем через «.backup» SQLite:
# простое копирование файла во время записи может дать испорченную копию.
set -euo pipefail
DATA=${FINANCE_DATA_DIR:-/opt/finance-bot/data}
DEST=${BACKUP_DIR:-/var/backups/finance-bot}
KEEP_DAYS=${KEEP_DAYS:-30}
stamp=$(date +%Y%m%d-%H%M)
umask 077
mkdir -p "$DEST"
python3 - "$DATA/finance.db" "$DEST/finance-$stamp.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
tar -czf "$DEST/receipts-$stamp.tar.gz" -C "$DATA" receipts 2>/dev/null || true
find "$DEST" -type f -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $DEST/finance-$stamp.db"
