#!/usr/bin/env bash
#
# Автоматическая установка Sync Admin на чистый VPS (Ubuntu 24.04 LTS).
# Запускать от root на СВЕЖЕМ сервере:
#
#   sudo bash deploy/install.sh your-domain.ru
#
# Скрипт делает всё то же самое, что расписано в deploy/README.md, но за
# один проход: системные пакеты, PostgreSQL, виртуальное окружение,
# .env с сгенерированными секретами, миграции, systemd-службы, Nginx.
# Повторный запуск безопасен — уже сделанные шаги пропускаются или
# выполняются заново без вреда (idempotent там, где это осмысленно).

set -euo pipefail

# ------------------------------------------------------------------
# 0. Проверки и параметры
# ------------------------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите скрипт от root: sudo bash deploy/install.sh your-domain.ru" >&2
    exit 1
fi

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    echo "Использование: sudo bash deploy/install.sh your-domain.ru" >&2
    exit 1
fi

SKIP_SSL="${SKIP_SSL:-0}"   # SKIP_SSL=1 sudo bash deploy/install.sh domain — если DNS ещё не настроен

APP_DIR="/opt/sync_admin"
APP_USER="syncadmin"
LOG_DIR="/var/log/sync_admin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"  # корень проекта (родитель deploy/)

c_green() { echo -e "\033[32m$1\033[0m"; }
c_yellow() { echo -e "\033[33m$1\033[0m"; }
c_red() { echo -e "\033[31m$1\033[0m"; }

step() { echo; c_green "=== $1 ==="; }

# ------------------------------------------------------------------
# 1. Системные пакеты
# ------------------------------------------------------------------

step "1/9 — Системные пакеты"

apt-get update -y
apt-get upgrade -y
apt-get install -y python3.12 python3.12-venv python3-pip \
    postgresql postgresql-contrib nginx certbot python3-certbot-nginx \
    rsync ufw

if ! command -v python3.12 >/dev/null 2>&1; then
    c_red "Python 3.12 не найден после установки — на Ubuntu 22.04 и старше его нет в стандартных репозиториях."
    c_red "Используйте Ubuntu 24.04 LTS (см. deploy/README.md) или подключите ppa:deadsnakes/ppa вручную."
    exit 1
fi

ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null
c_green "Файрвол настроен (SSH + HTTP/HTTPS)."

# ------------------------------------------------------------------
# 2. Системный пользователь
# ------------------------------------------------------------------

step "2/9 — Пользователь приложения"

if ! id "$APP_USER" >/dev/null 2>&1; then
    adduser --system --group --home "$APP_DIR" "$APP_USER"
    c_green "Пользователь $APP_USER создан."
else
    c_yellow "Пользователь $APP_USER уже существует — пропускаем."
fi

mkdir -p "$APP_DIR" "$LOG_DIR"
chown -R "$APP_USER:$APP_USER" "$LOG_DIR"

# ------------------------------------------------------------------
# 3. Копирование кода
# ------------------------------------------------------------------

step "3/9 — Копирование кода приложения в $APP_DIR"

rsync -a --delete \
    --exclude 'venv' --exclude '.git' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '*.db' --exclude '.env' \
    "$SCRIPT_DIR"/ "$APP_DIR"/

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
c_green "Код скопирован из $SCRIPT_DIR в $APP_DIR (существующий .env не тронут)."

# ------------------------------------------------------------------
# 4. Виртуальное окружение и зависимости
# ------------------------------------------------------------------

step "4/9 — Python venv и зависимости"

sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    python3.12 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
"
c_green "Зависимости установлены."

# ------------------------------------------------------------------
# 5. PostgreSQL
# ------------------------------------------------------------------

step "5/9 — PostgreSQL"

DB_NAME="sync_admin"
DB_USER="sync_user"

if [ -f "$APP_DIR/.env" ] && grep -q "^DATABASE_URL=" "$APP_DIR/.env"; then
    c_yellow ".env уже существует с DATABASE_URL — пароль БД не меняем, используем существующий."
    DB_PASSWORD="$(grep '^DATABASE_URL=' "$APP_DIR/.env" | sed -E 's#.*://[^:]+:([^@]+)@.*#\1#')"
else
    DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi

DB_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'")"
if [ "$DB_EXISTS" != "1" ]; then
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" >/dev/null
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" >/dev/null
    c_green "База данных $DB_NAME и пользователь $DB_USER созданы."
else
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" >/dev/null
    c_yellow "База данных $DB_NAME уже существовала — пароль пользователя синхронизирован с .env."
fi

# ------------------------------------------------------------------
# 6. Файл окружения (.env) — генерируем секреты автоматически
# ------------------------------------------------------------------

step "6/9 — Файл окружения"

if [ ! -f "$APP_DIR/.env" ]; then
    SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    SECRETS_ENCRYPTION_KEY="$(sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -c \
        'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

    # Secure-cookie только когда сервер будет за HTTPS. Если SSL пропущен
    # (SKIP_SSL=1), ставим 0 — иначе вход по http не сработает; после ручного
    # выпуска сертификата поправьте на 1 в $APP_DIR/.env и перезапустите службу.
    if [ "$SKIP_SSL" = "1" ]; then COOKIE_SECURE="0"; else COOKIE_SECURE="1"; fi
    cat > "$APP_DIR/.env" <<EOF
DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}
SESSION_SECRET=${SESSION_SECRET}
SECRETS_ENCRYPTION_KEY=${SECRETS_ENCRYPTION_KEY}
SESSION_COOKIE_SECURE=${COOKIE_SECURE}

# Склады на площадках теперь настраиваются в самой админке (страница
# «API-ключи», поле «ID склада на площадке» у каждого кабинета) — эти
# переменные больше не нужны, оставлены для совместимости со старыми
# версиями кода, можно удалить.
WAREHOUSE_ID_WB=
WAREHOUSE_ID_OZON=
WAREHOUSE_ID_KIT=

# FTP-канал с 1С (раздел 6 спецификации) — заполните перед тем, как
# полагаться на синхронизацию с 1С. Без этого веб-интерфейс и приём
# заказов работают, но перемещения в 1С создаваться не будут.
FTP_HOST=
FTP_USER=
FTP_PASSWORD=
FTP_DIR_TASKS=/sync/tasks
FTP_DIR_RESULTS=/sync/results
FTP_DIR_ARCHIVE=/sync/archive
EOF

    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    c_green ".env создан, секреты сгенерированы автоматически."
else
    c_yellow ".env уже существует — не перезаписываем, использованы текущие значения."
fi

# ------------------------------------------------------------------
# 7. Миграции и первый пользователь
# ------------------------------------------------------------------

step "7/9 — Миграции базы данных"

sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    source venv/bin/activate
    set -a; source .env; set +a
    alembic upgrade head
"
c_green "Миграции применены."

ADMIN_EXISTS_CHECK="$(sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    source venv/bin/activate
    set -a; source .env; set +a
    python -c \"
from app.database import SessionLocal
from app.models import User
db = SessionLocal()
print(db.query(User).count())
db.close()
\"
" 2>/dev/null | tail -1)"

if [ "$ADMIN_EXISTS_CHECK" = "0" ]; then
    ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
    sudo -u "$APP_USER" bash -c "
        cd '$APP_DIR'
        source venv/bin/activate
        set -a; source .env; set +a
        python create_admin_user.py admin '$ADMIN_PASSWORD'
    "
    c_green "Создан первый пользователь админки: admin / $ADMIN_PASSWORD"
    c_yellow "Запишите пароль — он больше нигде не сохранён в открытом виде. Смените после первого входа."
else
    c_yellow "В базе уже есть пользователи админки — новый не создаём."
fi

# ------------------------------------------------------------------
# 8. systemd-службы
# ------------------------------------------------------------------

step "8/9 — systemd-службы"

cp "$APP_DIR/deploy/sync-admin-web.service" /etc/systemd/system/
cp "$APP_DIR/deploy/sync-admin-scheduler.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now sync-admin-web
systemctl enable --now sync-admin-scheduler

sleep 2
if systemctl is-active --quiet sync-admin-web; then
    c_green "sync-admin-web запущен."
else
    c_red "sync-admin-web НЕ запустился — смотрите: journalctl -u sync-admin-web -n 50"
fi
if systemctl is-active --quiet sync-admin-scheduler; then
    c_green "sync-admin-scheduler запущен."
else
    c_red "sync-admin-scheduler НЕ запустился — смотрите: journalctl -u sync-admin-scheduler -n 50"
fi

# ------------------------------------------------------------------
# 9. Nginx и SSL
# ------------------------------------------------------------------

step "9/9 — Nginx и SSL"

sed "s/your-domain.ru/$DOMAIN/g" "$APP_DIR/deploy/nginx-sync-admin.conf" > /etc/nginx/sites-available/sync-admin
ln -sf /etc/nginx/sites-available/sync-admin /etc/nginx/sites-enabled/sync-admin

nginx -t
systemctl reload nginx
c_green "Nginx настроен на домен $DOMAIN (пока по HTTP)."

if [ "$SKIP_SSL" = "1" ]; then
    c_yellow "SKIP_SSL=1 — выпуск SSL пропущен. Когда DNS домена будет указывать на этот сервер, выполните:"
    c_yellow "  certbot --nginx -d $DOMAIN"
    c_yellow "После выпуска SSL поставьте SESSION_COOKIE_SECURE=1 в $APP_DIR/.env и: systemctl restart sync-admin-web"
else
    if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" --redirect; then
        c_green "SSL-сертификат выпущен и подключён."
    else
        c_yellow "Не удалось выпустить SSL автоматически (обычно из-за того, что DNS домена ещё не указывает на этот сервер)."
        c_yellow "Проверьте A-запись домена и повторите вручную: certbot --nginx -d $DOMAIN"
        c_yellow "ВАЖНО: пока сервер на голом HTTP, вход не сработает с SESSION_COOKIE_SECURE=1."
        c_yellow "Временно поставьте SESSION_COOKIE_SECURE=0 в $APP_DIR/.env (и обратно на 1 после выпуска SSL), затем: systemctl restart sync-admin-web"
    fi
fi

# ------------------------------------------------------------------
# Итог
# ------------------------------------------------------------------

echo
c_green "========================================"
c_green " Установка завершена"
c_green "========================================"
echo "Адрес:            https://$DOMAIN  (или http://, если SSL ещё не выпущен)"
echo "Логи веб:         journalctl -u sync-admin-web -f"
echo "Логи планировщика: journalctl -u sync-admin-scheduler -f"
echo "Проверка:         https://$DOMAIN/health"
echo
echo "Дальше: зайдите в /api-keys, добавьте кабинеты WB/Ozon/Kit и их ключи,"
echo "заполните FTP_* в $APP_DIR/.env для канала с 1С, перезапустите:"
echo "  systemctl restart sync-admin-web sync-admin-scheduler"
