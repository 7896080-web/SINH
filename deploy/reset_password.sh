#!/usr/bin/env bash
#
# Сброс пароля пользователя админки (или снятие блокировки после
# неудачных попыток входа) — оборачивает шаги "su syncadmin, активировать
# venv, подгрузить .env, запустить reset_password.py" в одну команду.
#
#   sudo bash deploy/reset_password.sh admin
#       — сгенерирует случайный пароль и один раз его выведет
#
#   sudo bash deploy/reset_password.sh admin мой-новый-пароль
#       — задаст указанный пароль
#
# Если пользователя с таким логином нет — будет создан новый (то же
# поведение, что у reset_password.py напрямую).

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите от root: sudo bash deploy/reset_password.sh <логин> [пароль]" >&2
    exit 1
fi

APP_DIR="/opt/sync_admin"
APP_USER="syncadmin"

c_green() { echo -e "\033[32m$1\033[0m"; }
c_yellow() { echo -e "\033[33m$1\033[0m"; }

USERNAME="${1:-}"
if [ -z "$USERNAME" ]; then
    echo "Использование: sudo bash deploy/reset_password.sh <логин> [пароль]" >&2
    exit 1
fi

if [ ! -f "$APP_DIR/.env" ]; then
    echo "$APP_DIR/.env не найден — приложение ещё не установлено." >&2
    exit 1
fi

PASSWORD="${2:-}"
GENERATED=0
if [ -z "$PASSWORD" ]; then
    PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
    GENERATED=1
fi

sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    source venv/bin/activate
    set -a; source .env; set +a
    python reset_password.py '$USERNAME' '$PASSWORD'
"

echo
if [ "$GENERATED" -eq 1 ]; then
    c_yellow "Пароль сгенерирован автоматически — запишите, больше нигде не показывается:"
    c_green "  $USERNAME / $PASSWORD"
else
    c_green "Пароль для «$USERNAME» обновлён на указанный."
fi
