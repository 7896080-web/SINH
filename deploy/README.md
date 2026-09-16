# Развёртывание на VPS/облачном сервере reg.ru (Linux — альтернатива)

> ⚠️ **Актуальный целевой сервер — Windows Server** (WIN-Q0QVP2SM59L,
> `136.243.92.95`), где уже работает 1С, и обмен идёт через локальную папку
> `C:\sync` (не FTP). Этот файл — Linux-вариант, оставлен как альтернатива;
> под текущий Windows-таргет он не применяется. Windows-развёртывание
> (служба через NSSM/планировщик задач, PostgreSQL для Windows) — отдельная задача.

**Важно:** это инструкция именно под VPS/VDS или облачный сервер (`reg.cloud`),
**не** под тариф «Хостинг с поддержкой Python». Обычный виртуальный хостинг
reg.ru работает через Passenger WSGI (несовместимо с ASGI-фреймворком
FastAPI), даёт только MySQL (наш код рассчитан на PostgreSQL) и не позволяет
держать постоянно работающий процесс — а вся синхронизация построена именно
на нём (`app/workers/scheduler.py`). Подробности — в чате, где принималось
это решение.

## 1. Заказ сервера

- В панели reg.ru: **VPS и VDS серверы** → облачный сервер.
- ОС: **Ubuntu 24.04 LTS** — в её репозиториях уже есть Python 3.12 «из коробки»,
  не нужно подключать сторонние PPA (на 22.04 в системе только 3.10).
- Минимальная конфигурация достаточна: 1-2 vCPU, 2 ГБ RAM — нагрузка на
  сервис некритичная (несколько кабинетов площадок, не тысячи запросов в секунду).

## 2. Базовая подготовка сервера

```bash
apt update && apt upgrade -y
apt install -y python3.12 python3.12-venv python3-pip \
    postgresql postgresql-contrib nginx certbot python3-certbot-nginx \
    git ufw

# Отдельный непривилегированный пользователь — не запускаем приложение от root
adduser --system --group --home /opt/sync_admin syncadmin

# Файрвол — только то, что реально нужно снаружи
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
```

Исходящие соединения к API WB/Ozon/Kit на VPS по умолчанию не блокируются.
(Обмен с 1С — через локальную папку, сетевого соединения не требует.)

## 3. PostgreSQL

```bash
sudo -u postgres psql -c "CREATE USER sync_user WITH PASSWORD 'замените_на_свой_пароль';"
sudo -u postgres psql -c "CREATE DATABASE sync_admin OWNER sync_user;"
```

## 4. Код приложения

```bash
su - syncadmin
cd /opt/sync_admin   # уже существует, владелец syncadmin — папку создать от root заранее: mkdir -p /opt/sync_admin && chown syncadmin:syncadmin /opt/sync_admin

git clone <ваш_репозиторий> .        # либо просто скопировать файлы проекта
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 5. Файл окружения

```bash
cp .env.example .env
nano .env
```

Заполнить реальными значениями — `DATABASE_URL` (с паролем из шага 3),
`SESSION_SECRET`, `SECRETS_ENCRYPTION_KEY` (сгенерировать командой из
`.env.example`), `SYNC_DIR_TASKS/RESULTS/ARCHIVE` — пути папки обмена с 1С.

## 6. Миграции и первый пользователь

```bash
source venv/bin/activate
alembic upgrade head
python create_admin_user.py admin ваш_пароль
```

## 7. systemd-службы

```bash
exit  # обратно под root/sudo

mkdir -p /var/log/sync_admin && chown syncadmin:syncadmin /var/log/sync_admin

cp deploy/sync-admin-web.service /etc/systemd/system/
cp deploy/sync-admin-scheduler.service /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now sync-admin-web
systemctl enable --now sync-admin-scheduler

systemctl status sync-admin-web
systemctl status sync-admin-scheduler
```

## 8. Nginx и SSL

```bash
cp deploy/nginx-sync-admin.conf /etc/nginx/sites-available/sync-admin
# поправить server_name на реальный домен
ln -s /etc/nginx/sites-available/sync-admin /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

certbot --nginx -d your-domain.ru   # бесплатный SSL, Let's Encrypt
```

## 9. Проверка

- `https://your-domain.ru/health` — должен ответить 200 (или 503, если
  воркеры ещё ни разу не отчитались — это нормально сразу после установки).
- `https://your-domain.ru/login` — страница входа.
- `journalctl -u sync-admin-scheduler -f` — живой лог воркеров.

## Обновление кода в будущем — рекомендуемый способ

```bash
sudo bash deploy/safe_deploy.sh
```

Делает бэкап кода и базы, гоняет тесты до деплоя, проверяет здоровье после
и автоматически откатывает код при сбое. Подробности — в `CLAUDE.md` и
корневом `README.md`.

Ручной способ без бэкапа/тестов/автооткате (только для стейджа/отладки):

```bash
sudo bash deploy/update.sh
```

Перезапуск `sync-admin-scheduler` обязателен не только при обновлении
кода, но и **при добавлении нового кабинета** в разделе «API-ключи» —
задания APScheduler регистрируются один раз при старте процесса
(см. комментарий в `sync-admin-scheduler.service`). `safe_deploy.sh` и
`update.sh` оба перезапускают службу, так что после любого из них
это уже не нужно делать отдельно.
