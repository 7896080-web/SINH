#!/usr/bin/env bash
# Постоянная страница настроек под паролем (ключ Claude API, токен бота, id пользователей).
#
#   sudo bash deploy/settings.sh              — включить (при первом запуске спросит пароль)
#   sudo bash deploy/settings.sh --password   — сменить пароль
#   sudo bash deploy/settings.sh --off        — выключить страницу и закрыть порт
#
# Страница: https://IP_СЕРВЕРА:8765 (порт меняется: PORT=9443 sudo bash deploy/settings.sh)
set -euo pipefail
APP=${APP:-/opt/finance-bot}
DIR="$APP/settings"
PORT=${PORT:-8765}
SVC_USER=finance-settings
fail() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }
ufw_active() { command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; }

[ "$(id -u)" = 0 ] || fail "запустите через sudo: sudo bash deploy/settings.sh"
[ -x "$APP/venv/bin/python" ] || fail "бот не установлен — сначала: sudo bash deploy/install.sh"

if [ "${1:-}" = "--off" ]; then
    systemctl disable --now finance-bot-settings 2>/dev/null || true
    old_port=$(sed -n 's/^PORT=//p' "$DIR/port.env" 2>/dev/null || echo "$PORT")
    ufw_active && ufw delete allow "${old_port:-$PORT}/tcp" >/dev/null 2>&1 || true
    echo "Страница настроек выключена, порт ${old_port:-$PORT} закрыт."
    exit 0
fi

# Пользователь службы: читает свою папку, пишет только .env.
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$DIR"
[ -f "$APP/.env" ] || cp "$APP/deploy/../.env.example" "$APP/.env" 2>/dev/null || touch "$APP/.env"
# .env: пишет служба настроек, читает бот (группа finance-bot); другим — ничего.
chown "$SVC_USER":finance-bot "$APP/.env"
chmod 640 "$APP/.env"

if [ ! -f "$DIR/password" ] || [ "${1:-}" = "--password" ]; then
    echo
    echo "Задайте пароль для страницы настроек (не короче 10 символов)."
    "$APP/venv/bin/python" -m finance.settings_web set-password --dir "$DIR"
fi

if [ ! -f "$DIR/cert.pem" ] || ! openssl x509 -checkend 2592000 -noout -in "$DIR/cert.pem" >/dev/null 2>&1; then
    # Самоподписанный сертификат на 2 года (перевыпускается, если осталось < 30 дней).
    openssl req -x509 -newkey rsa:2048 -nodes -days 730 -subj "/CN=finance-bot-settings" \
        -keyout "$DIR/key.pem" -out "$DIR/cert.pem" >/dev/null 2>&1 \
        || fail "не удалось создать сертификат (нужен openssl)"
    echo "Создан сертификат страницы (самоподписанный, на 2 года)."
fi
echo "PORT=$PORT" > "$DIR/port.env"
chown -R "$SVC_USER":"$SVC_USER" "$DIR"
chmod 700 "$DIR"
chmod 600 "$DIR"/*

cp "$APP/deploy/finance-bot-settings.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable finance-bot-settings >/dev/null
systemctl restart finance-bot-settings
sleep 2
systemctl is-active --quiet finance-bot-settings \
    || { journalctl -u finance-bot-settings -n 20 --no-pager; fail "страница не запустилась"; }
if ufw_active; then
    ufw allow "$PORT/tcp" >/dev/null
    echo "Порт $PORT открыт в ufw."
fi

HOST=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<MSG

Страница настроек работает:  https://${HOST:-IP_СЕРВЕРА}:$PORT

  • Браузер предупредит о сертификате (он самоподписанный):
    «Дополнительно» → «Перейти на сайт». Соединение шифруется.
  • Вход — по вашему паролю. 5 ошибок с одного адреса — блокировка на 15 минут.
  • После сохранения бот сам перезапустится примерно через 20 секунд.
  • Сменить пароль:  sudo bash $APP/deploy/settings.sh --password
  • Выключить:       sudo bash $APP/deploy/settings.sh --off
  • Журнал входов:   journalctl -u finance-bot-settings
MSG
