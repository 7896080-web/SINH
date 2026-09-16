#!/usr/bin/env bash
#
# Обновление уже установленного Sync Admin новой версией кода.
# Запускать от root, находясь в каталоге с обновлённым проектом:
#
#   sudo bash deploy/update.sh
#
# В отличие от install.sh — не трогает .env, базу данных и пользователей,
# только код, зависимости, миграции и перезапуск служб.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите от root: sudo bash deploy/update.sh" >&2
    exit 1
fi

APP_DIR="/opt/sync_admin"
APP_USER="syncadmin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

c_green() { echo -e "\033[32m$1\033[0m"; }
c_yellow() { echo -e "\033[33m$1\033[0m"; }

if [ ! -f "$APP_DIR/.env" ]; then
    echo "$APP_DIR/.env не найден — похоже, приложение ещё не установлено." >&2
    echo "Сначала выполните: sudo bash deploy/install.sh your-domain.ru" >&2
    exit 1
fi

echo "=== Копирование обновлённого кода ==="
rsync -a --delete \
    --exclude 'venv' --exclude '.git' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '*.db' --exclude '.env' \
    "$SCRIPT_DIR"/ "$APP_DIR"/
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
c_green "Код обновлён."

echo "=== Обновление зависимостей ==="
sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    source venv/bin/activate
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
"
c_green "Зависимости обновлены."

echo "=== Применение миграций ==="
sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    source venv/bin/activate
    set -a; source .env; set +a
    alembic upgrade head
"
c_green "Миграции применены."

echo "=== Перезапуск служб ==="
systemctl daemon-reload
systemctl restart sync-admin-web
systemctl restart sync-admin-scheduler

sleep 2
if systemctl is-active --quiet sync-admin-web && systemctl is-active --quiet sync-admin-scheduler; then
    c_green "Обновление завершено, обе службы работают."
else
    echo "Что-то не запустилось — проверьте:" >&2
    echo "  journalctl -u sync-admin-web -n 50" >&2
    echo "  journalctl -u sync-admin-scheduler -n 50" >&2
    exit 1
fi

c_yellow "Напоминание: если в этом обновлении добавлен новый кабинет в БД вручную (не через миграцию) — перезапуск планировщика выше уже это учёл."
