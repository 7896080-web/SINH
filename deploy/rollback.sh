#!/usr/bin/env bash
#
# Ручной откат к последнему (или указанному) бэкапу.
#
#   sudo bash deploy/rollback.sh                 — откатить код к последнему бэкапу
#   sudo bash deploy/rollback.sh --with-db        — откатить код И базу (СТИРАЕТ
#                                                    все реальные данные после бэкапа!)
#   sudo bash deploy/rollback.sh 20260115-093000  — откатить к конкретному бэкапу
#   sudo bash deploy/rollback.sh 20260115-093000 --with-db

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите от root: sudo bash deploy/rollback.sh" >&2
    exit 1
fi

APP_DIR="/opt/sync_admin"
APP_USER="syncadmin"
BACKUPS_ROOT="/opt/sync_admin_backups"

c_green() { echo -e "\033[32m$1\033[0m"; }
c_yellow() { echo -e "\033[33m$1\033[0m"; }
c_red() { echo -e "\033[31m$1\033[0m"; }

WITH_DB=0
TARGET=""
for arg in "$@"; do
    if [ "$arg" = "--with-db" ]; then
        WITH_DB=1
    else
        TARGET="$arg"
    fi
done

if [ -z "$TARGET" ]; then
    if [ ! -e "$BACKUPS_ROOT/latest" ]; then
        echo "Бэкапов ещё нет ($BACKUPS_ROOT/latest не существует)." >&2
        exit 1
    fi
    BACKUP_PATH="$(readlink -f "$BACKUPS_ROOT/latest")"
else
    BACKUP_PATH="$BACKUPS_ROOT/$TARGET"
    if [ ! -d "$BACKUP_PATH" ]; then
        echo "Бэкап $BACKUP_PATH не найден. Доступные:" >&2
        find "$BACKUPS_ROOT" -maxdepth 1 -mindepth 1 -type d -name '20*' -printf '%f\n' >&2
        exit 1
    fi
fi

echo "Откатываюсь к бэкапу: $BACKUP_PATH"
if [ "$WITH_DB" -eq 1 ]; then
    c_yellow "С восстановлением базы данных — это СОТРЁТ все реальные данные,"
    c_yellow "принятые после создания этого бэкапа (заказы, изменения остатков)."
    read -r -p "Точно продолжить? Наберите 'да' для подтверждения: " CONFIRM
    if [ "$CONFIRM" != "да" ]; then
        echo "Отменено."
        exit 0
    fi
fi

# Код
rm -rf "$APP_DIR.rollback_tmp"
mkdir -p "$APP_DIR.rollback_tmp"
tar xzf "$BACKUP_PATH/code.tar.gz" -C "$APP_DIR.rollback_tmp"
rsync -a --delete --exclude '.env' \
    "$APP_DIR.rollback_tmp/$(basename "$APP_DIR")/" "$APP_DIR/"
rm -rf "$APP_DIR.rollback_tmp"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
c_green "Код восстановлен."

# Зависимости на случай, если откатываемся на версию со старым requirements.txt
sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    source venv/bin/activate
    pip install -r requirements.txt -q
"

# База — только если явно попросили
if [ "$WITH_DB" -eq 1 ]; then
    if [ ! -f "$BACKUP_PATH/db.sql.gz" ]; then
        c_red "В этом бэкапе нет db.sql.gz — база не восстановлена."
    else
        DATABASE_URL="$(grep '^DATABASE_URL=' "$APP_DIR/.env" | cut -d= -f2-)"
        DB_USER="$(echo "$DATABASE_URL" | sed -E 's#postgresql://([^:]+):.*#\1#')"
        DB_PASS="$(echo "$DATABASE_URL" | sed -E 's#postgresql://[^:]+:([^@]+)@.*#\1#')"
        DB_HOST="$(echo "$DATABASE_URL" | sed -E 's#.*@([^:/]+).*#\1#')"
        DB_NAME="$(echo "$DATABASE_URL" | sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')"

        gunzip -c "$BACKUP_PATH/db.sql.gz" | PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -U "$DB_USER" "$DB_NAME"
        c_green "База данных восстановлена из $BACKUP_PATH/db.sql.gz."
    fi
else
    c_yellow "База данных НЕ трогалась (запустите с --with-db, если нужно и её откатить)."
fi

systemctl daemon-reload
systemctl restart sync-admin-web sync-admin-scheduler
c_green "Службы перезапущены. Откат завершён."
