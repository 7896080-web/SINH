#!/usr/bin/env bash
# Временная страница настройки: Telegram id пользователей, ключ Claude API, токен бота.
#
#   sudo bash deploy/setup.sh            — страница только на сервере (через SSH-туннель)
#   sudo bash deploy/setup.sh --public   — по HTTPS с временным сертификатом (с телефона)
#
# Ссылка одноразовая, страница закрывается после сохранения или через 15 минут.
set -euo pipefail
APP=${APP:-/opt/finance-bot}
PORT=${PORT:-8765}
fail() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "запустите через sudo: sudo bash deploy/setup.sh"
[ -x "$APP/venv/bin/python" ] || fail "бот не установлен — сначала: sudo bash deploy/install.sh"
[ -f "$APP/.env" ] || cp "$APP/.env.example" "$APP/.env" 2>/dev/null || touch "$APP/.env"

HOST=$(hostname -I 2>/dev/null | awk '{print $1}')
ARGS=(--env "$APP/.env" --port "$PORT" --host-hint "${HOST:-IP_СЕРВЕРА}")
OPENED_FW=0
TMP=""
cleanup() {
    [ -n "$TMP" ] && rm -rf "$TMP"
    if [ "$OPENED_FW" = 1 ]; then
        ufw delete allow "$PORT/tcp" >/dev/null 2>&1 || true
        echo "Порт $PORT снова закрыт."
    fi
}
trap cleanup EXIT

if [ "${1:-}" = "--public" ]; then
    command -v openssl >/dev/null || fail "нужен openssl (apt install openssl)"
    TMP=$(mktemp -d)
    chmod 700 "$TMP"
    # Временный самоподписанный сертификат на сутки: ключи идут по шифрованному каналу.
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=finance-bot-setup" \
        -keyout "$TMP/key.pem" -out "$TMP/cert.pem" >/dev/null 2>&1 \
        || fail "не удалось создать временный сертификат"
    ARGS+=(--public --cert "$TMP/cert.pem" --key "$TMP/key.pem")
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow "$PORT/tcp" >/dev/null
        OPENED_FW=1
        echo "Порт $PORT временно открыт в ufw — закроется после настройки."
    fi
fi

cd "$APP"
"$APP/venv/bin/python" -m finance.setup_web "${ARGS[@]}"
