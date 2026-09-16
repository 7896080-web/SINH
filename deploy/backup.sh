#!/usr/bin/env bash
#
# Бэкап кода и базы данных перед обновлением/рискованной операцией.
# Используется deploy/safe_deploy.sh, но можно запускать и отдельно:
#
#   sudo bash deploy/backup.sh
#
# Кладёт всё в /opt/sync_admin_backups/<timestamp>/ и обновляет симлинк
# /opt/sync_admin_backups/latest на самый свежий бэкап.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите от root: sudo bash deploy/backup.sh" >&2
    exit 1
fi

APP_DIR="/opt/sync_admin"
BACKUPS_ROOT="/opt/sync_admin_backups"
KEEP_LAST=10   # сколько последних бэкапов хранить, остальное — удаляется

if [ ! -f "$APP_DIR/.env" ]; then
    echo "$APP_DIR/.env не найден — нечего бэкапить, приложение ещё не установлено." >&2
    exit 1
fi

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUPS_ROOT/$TIMESTAMP"
mkdir -p "$BACKUP_DIR"

c_green() { echo -e "\033[32m$1\033[0m"; }

# ------------------------------------------------------------------
# Код
# ------------------------------------------------------------------

tar czf "$BACKUP_DIR/code.tar.gz" \
    --exclude='venv' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.db' \
    -C "$(dirname "$APP_DIR")" "$(basename "$APP_DIR")"
c_green "Код сохранён: $BACKUP_DIR/code.tar.gz"

# ------------------------------------------------------------------
# База данных
# ------------------------------------------------------------------

DATABASE_URL="$(grep '^DATABASE_URL=' "$APP_DIR/.env" | cut -d= -f2-)"

if [[ "$DATABASE_URL" == postgresql://* ]]; then
    # Разбираем postgresql://user:password@host:port/dbname
    DB_USER="$(echo "$DATABASE_URL" | sed -E 's#postgresql://([^:]+):.*#\1#')"
    DB_PASS="$(echo "$DATABASE_URL" | sed -E 's#postgresql://[^:]+:([^@]+)@.*#\1#')"
    DB_HOST="$(echo "$DATABASE_URL" | sed -E 's#.*@([^:/]+).*#\1#')"
    DB_NAME="$(echo "$DATABASE_URL" | sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')"

    PGPASSWORD="$DB_PASS" pg_dump -h "$DB_HOST" -U "$DB_USER" "$DB_NAME" \
        | gzip > "$BACKUP_DIR/db.sql.gz"
    c_green "База данных сохранена: $BACKUP_DIR/db.sql.gz"
else
    echo "DATABASE_URL не похож на PostgreSQL — бэкап базы пропущен (только код)." >&2
fi

# ------------------------------------------------------------------
# Указатель на последний бэкап + ротация старых
# ------------------------------------------------------------------

ln -sfn "$BACKUP_DIR" "$BACKUPS_ROOT/latest"

cd "$BACKUPS_ROOT"
# Сортировка по времени изменения, только каталоги с именем-таймстампом —
# glob вместо ls|grep, чтобы имена не ломали парсинг (замечание shellcheck SC2010)
mapfile -t ALL_BACKUPS < <(find . -maxdepth 1 -mindepth 1 -type d -name '20*' -printf '%T@ %f\n' | sort -rn | cut -d' ' -f2)
if [ "${#ALL_BACKUPS[@]}" -gt "$KEEP_LAST" ]; then
    for old in "${ALL_BACKUPS[@]:$KEEP_LAST}"; do
        rm -rf "$old"
        echo "Удалён старый бэкап: $old"
    done
fi

c_green "Готово: $BACKUP_DIR (симлинк latest обновлён)"
