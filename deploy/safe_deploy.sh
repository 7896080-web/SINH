#!/usr/bin/env bash
#
# Безопасное обновление уже работающего прода. Единственный способ
# обновления, рассчитанный на запуск агентом (Claude Code) без присмотра —
# см. CLAUDE.md. В отличие от update.sh: делает бэкап, гоняет тесты ДО
# того, как тронуть прод, и автоматически откатывает код при неудачном
# health-check после деплоя.
#
#   sudo bash deploy/safe_deploy.sh
#
# База при автооткате НЕ восстанавливается — см. объяснение ниже, перед
# шагом отката.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите от root: sudo bash deploy/safe_deploy.sh" >&2
    exit 1
fi

APP_DIR="/opt/sync_admin"
APP_USER="syncadmin"
BACKUPS_ROOT="/opt/sync_admin_backups"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

c_green() { echo -e "\033[32m$1\033[0m"; }
c_yellow() { echo -e "\033[33m$1\033[0m"; }
c_red() { echo -e "\033[31m$1\033[0m"; }
step() { echo; c_green "=== $1 ==="; }

if [ ! -f "$APP_DIR/.env" ]; then
    echo "$APP_DIR/.env не найден — приложение ещё не установлено, используйте deploy/install.sh." >&2
    exit 1
fi

# ------------------------------------------------------------------
# 1. Тесты — ДО того, как трогать прод
# ------------------------------------------------------------------

step "1/5 — Тесты новой версии кода (ещё не тронули прод)"

TEST_VENV="$(mktemp -d)/venv"
python3.12 -m venv "$TEST_VENV" 2>/dev/null || python3 -m venv "$TEST_VENV"
# shellcheck disable=SC1091
source "$TEST_VENV/bin/activate"
pip install --upgrade pip -q
pip install -r "$SCRIPT_DIR/requirements.txt" -q
pip install pytest -q

TEST_DB="$(mktemp -d)/test.db"
export DATABASE_URL="sqlite:///$TEST_DB"
export SESSION_SECRET="deploy-test-secret-32-characters-x"
export SECRETS_ENCRYPTION_KEY
SECRETS_ENCRYPTION_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

if ! (cd "$SCRIPT_DIR" && python3 -m pytest tests/ -q); then
    c_red "Тесты не прошли — деплой ОТМЕНЁН, прод не тронут."
    deactivate
    exit 1
fi
c_green "Все тесты прошли."
deactivate
unset DATABASE_URL SESSION_SECRET SECRETS_ENCRYPTION_KEY

# ------------------------------------------------------------------
# 2. Бэкап
# ------------------------------------------------------------------

step "2/5 — Бэкап текущего кода и базы"
bash "$APP_DIR/deploy/backup.sh"
BACKUP_PATH="$(readlink -f "$BACKUPS_ROOT/latest")"

# ------------------------------------------------------------------
# 3. Деплой (то же самое, что update.sh)
# ------------------------------------------------------------------

step "3/5 — Копирование кода и обновление зависимостей"

rsync -a --delete \
    --exclude 'venv' --exclude '.git' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '*.db' --exclude '.env' \
    "$SCRIPT_DIR"/ "$APP_DIR"/
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    source venv/bin/activate
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
"
c_green "Код и зависимости обновлены."

step "3/5 — Миграции"
sudo -u "$APP_USER" bash -c "
    cd '$APP_DIR'
    source venv/bin/activate
    set -a; source .env; set +a
    alembic upgrade head
"
c_green "Миграции применены."

systemctl daemon-reload
systemctl restart sync-admin-web
systemctl restart sync-admin-scheduler
c_green "Службы перезапущены."

# ------------------------------------------------------------------
# 4. Health-check
# ------------------------------------------------------------------

step "4/5 — Проверка здоровья после деплоя"

HEALTHY=1

sleep 3
for i in 1 2 3 4 5; do
    if systemctl is-active --quiet sync-admin-web && systemctl is-active --quiet sync-admin-scheduler; then
        break
    fi
    if [ "$i" -eq 5 ]; then
        c_red "Службы не поднялись после перезапуска."
        HEALTHY=0
    fi
    sleep 2
done

# /login не зависит от воркеров — если он не отвечает 200, значит сломан
# сам веб-процесс, а не просто "воркеры ещё не успели отчитаться"
LOGIN_STATUS="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login || echo 000)"
if [ "$LOGIN_STATUS" != "200" ]; then
    c_red "GET /login вернул $LOGIN_STATUS вместо 200."
    HEALTHY=0
fi

if [ "$HEALTHY" -eq 1 ]; then
    c_green "Health-check пройден."
else
    step "5/5 — АВТООТКАТ КОДА"
    c_yellow "Health-check провален — откатываю код к версии из бэкапа $BACKUP_PATH"
    c_yellow "База данных НЕ откатывается автоматически: если между бэкапом и этим"
    c_yellow "моментом успели прийти реальные заказы, откат базы их бы стёр. Если"
    c_yellow "проблема именно в структуре данных (сломанная миграция) — разберитесь"
    c_yellow "руками и при необходимости восстановите базу отдельно:"
    c_yellow "  gunzip -c $BACKUP_PATH/db.sql.gz | sudo -u postgres psql <имя_базы>"

    rm -rf "$APP_DIR.rollback_tmp"
    mkdir -p "$APP_DIR.rollback_tmp"
    tar xzf "$BACKUP_PATH/code.tar.gz" -C "$APP_DIR.rollback_tmp"
    rsync -a --delete "$APP_DIR.rollback_tmp/$(basename "$APP_DIR")/" "$APP_DIR/"
    rm -rf "$APP_DIR.rollback_tmp"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"

    systemctl restart sync-admin-web sync-admin-scheduler

    c_red "Код откачен. Деплой ЗАВЕРШИЛСЯ НЕУДАЧЕЙ — новая версия не в проде."
    exit 1
fi

step "5/5 — Готово"
c_green "Деплой успешен. Бэкап на случай ручного отката: $BACKUP_PATH"
