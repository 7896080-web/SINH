#!/usr/bin/env bash
# Установка и обновление финансового помощника (Telegram-бот) на сервере.
#
#   sudo bash deploy/install.sh          — из распакованной папки finance_assistant
#
# Первая установка: создаёт пользователя службы, ставит код в /opt/finance-bot,
# виртуальное окружение, службы systemd и ежедневный бэкап, кладёт .env-шаблон.
# Пока в .env не вписаны токены, бот не запускается — скрипт скажет, что сделать.
# Повторный запуск = обновление: код и зависимости заменяются, .env и данные
# (data/ — базы и скриншоты пользователей) не трогаются. Перед обновлением
# делается бэкап баз.
set -euo pipefail

APP=${APP:-/opt/finance-bot}
SERVICE_USER=${SERVICE_USER:-finance-bot}
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
SKIP_SYSTEMD=${SKIP_SYSTEMD:-0}   # для проверки скрипта без systemd
SKIP_USER=${SKIP_USER:-0}         # для проверки скрипта без useradd/chown
SRC=$(cd "$(dirname "$0")/.." && pwd)

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$SKIP_USER" = 1 ] || [ "$(id -u)" = 0 ] || fail "запустите через sudo: sudo bash deploy/install.sh"
[ -f "$SRC/finance/bot.py" ] || fail "не нашёл finance/bot.py рядом со скриптом — запускайте из распакованной папки"

say "Проверяю Python"
command -v python3 >/dev/null || fail "нет python3 (Ubuntu/Debian: apt install python3 python3-venv)"
python3 - <<'PY' || fail "нужен Python 3.10 или новее"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
python3 -c "import venv, ensurepip" 2>/dev/null \
    || fail "нет модуля venv (Ubuntu/Debian: apt install python3-venv)"
echo "   $(python3 --version)"

if [ "$SKIP_USER" != 1 ] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
    say "Создаю пользователя службы $SERVICE_USER"
    useradd --system --home-dir "$APP" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

UPDATE=0
[ -d "$APP/finance" ] && UPDATE=1

if [ "$UPDATE" = 1 ] && [ -d "$APP/data" ] && [ -x "$APP/deploy/finance-bot-backup.sh" ]; then
    say "Обновление: сначала бэкап баз"
    FINANCE_DATA_DIR="$APP/data" "$APP/deploy/finance-bot-backup.sh" || fail "бэкап не удался — обновление остановлено"
fi

say "Копирую код в $APP"
mkdir -p "$APP"
rm -rf "$APP/finance.new" "$APP/deploy.new"
cp -r "$SRC/finance" "$APP/finance.new"
cp -r "$SRC/deploy" "$APP/deploy.new"
find "$APP/finance.new" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$APP/finance" "$APP/deploy"
mv "$APP/finance.new" "$APP/finance"
mv "$APP/deploy.new" "$APP/deploy"
cp "$SRC/requirements.txt" "$APP/requirements.txt"
[ -f "$SRC/README.md" ] && cp "$SRC/README.md" "$APP/README.md"
chmod +x "$APP/deploy/"*.sh

say "Ставлю зависимости (виртуальное окружение $APP/venv)"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"
(cd "$APP" && "$APP/venv/bin/python" -c "import finance.bot") \
    || fail "код не импортируется — см. ошибку выше"

say "Папка данных и настройки"
mkdir -p "$APP/data"
if [ ! -f "$APP/.env" ]; then
    cp "$SRC/.env.example" "$APP/.env"
    echo "   создан $APP/.env из шаблона — впишите токены (см. ниже)"
fi
if [ "$SKIP_USER" != 1 ]; then
    # Код и настройки — root (служба их не меняет), данные — только служба.
    chown -R root:root "$APP/finance" "$APP/deploy" "$APP/venv" "$APP/requirements.txt"
    chown root:"$SERVICE_USER" "$APP/.env"
    chown -R "$SERVICE_USER": "$APP/data"
fi
chmod 640 "$APP/.env"
chmod 700 "$APP/data"

env_value() { sed -n "s/^$1=//p" "$APP/.env" | tail -1 | tr -d '[:space:]"'"'"; }
READY=1
for key in TELEGRAM_BOT_TOKEN ANTHROPIC_API_KEY; do
    if [ -z "$(env_value "$key")" ]; then
        READY=0
        echo "   не заполнено в .env: $key"
    fi
done
# Список пользователей может быть пуст: бот запустится и каждому ответит его id.
NO_USERS=0
[ -n "$(env_value ALLOWED_USER_IDS)" ] || NO_USERS=1

if [ "$SKIP_SYSTEMD" != 1 ]; then
    say "Службы systemd"
    cp "$APP/deploy/finance-bot.service" "$APP/deploy/finance-bot-backup.service" \
       "$APP/deploy/finance-bot-backup.timer" "$SYSTEMD_DIR/"
    systemctl daemon-reload
    systemctl enable --now finance-bot-backup.timer >/dev/null
    systemctl enable finance-bot >/dev/null
    if [ "$READY" = 1 ]; then
        systemctl restart finance-bot
        sleep 3
        if systemctl is-active --quiet finance-bot; then
            echo "   finance-bot запущен"
        else
            journalctl -u finance-bot -n 30 --no-pager || true
            fail "служба не запустилась — выше последние строки журнала"
        fi
    fi
fi

if [ "$READY" = 1 ] && [ "$NO_USERS" = 1 ]; then
    say "Бот запущен, осталось добавить пользователей"
    cat <<EOF
   1. Каждый из двух пользователей пишет боту в Telegram что угодно —
      бот ответит «Доступ закрыт. Ваш Telegram id: …».
   2. Впишите оба id на странице настройки:
        sudo bash $APP/deploy/setup.sh --public
      (первым — тот, кому достанутся данные прежней общей базы, если она была)
EOF
elif [ "$READY" = 1 ]; then
    say "Готово"
    [ "$UPDATE" = 1 ] && echo "   Обновлено. Данные и .env не тронуты."
    cat <<EOF
   Журнал:   journalctl -u finance-bot -f
   Бэкап:    каждый день в 03:30 → /var/backups/finance-bot
             (вручную: sudo systemctl start finance-bot-backup)
EOF
else
    say "Осталось вписать настройки"
    cat <<EOF
   Откройте страницу настройки — на ней ключ Claude API, токен бота и
   Telegram id обоих пользователей (с кнопкой «Кто писал боту»):

      sudo bash $APP/deploy/setup.sh            — через SSH-туннель (надёжнее)
      sudo bash $APP/deploy/setup.sh --public   — с телефона, по HTTPS

   После сохранения бот запустится сам.
   (Или вручную: sudo nano $APP/.env и снова sudo bash deploy/install.sh)
EOF
fi
