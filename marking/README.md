# Маркировка и поставки

Программа для поставок на Lamoda: входящий файл → проверка остатка и
перемещение в 1С → УПД и стикеры коробов; справочник GTIN и карточки
Нацкаталога; этикетки из файла кодов. Работает на **рабочем компьютере с
КриптоПро**, с 1С на сервере связана только файлами по SFTP.

**Сейчас сделаны этапы 2–3 ТЗ.** Входа в Честный знак по сертификату, заказа
кодов и ввода в оборот ещё нет — это этап 4.

- **Состояние, что проверено и что дальше — `HANDOFF.md`** (читать первым)
- Правила разработки: `CLAUDE.md`
- Развёртывание — что и на какой машине: `deploy/РАЗВЁРТЫВАНИЕ.md`
- Установка и обновление на рабочем компьютере: `deploy/README.md`
- Сервер (SFTP к папкам 1С): `deploy/SFTP_1C.md`
- Обновление обработки 1С: `1c/ОБНОВЛЕНИЕ_ОБРАБОТКИ.md`
- Задание: `../ТЗ_МАРКИРОВКА.md`, сценарий оператора: `../СЦЕНАРИЙ_МАРКИРОВКА.md`

## Разработка

Один процесс: веб и фоновая работа вместе (`workers/background.py`).

Linux / macOS:
```bash
cd marking
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=sqlite:///./dev.db MARKING_SESSION_SECRET=dev-secret \
  MARKING_SECRETS_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
alembic upgrade head && python create_user.py admin devpassword
uvicorn markapp.main:app --reload --port 8011
```

Windows (PowerShell), в КЛОНЕ репозитория, а не в `C:\marking`:
```powershell
cd C:\dev\sinh\marking
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DATABASE_URL="sqlite:///./dev.db"; $env:MARKING_SESSION_SECRET="dev-secret"
$env:MARKING_SECRETS_KEY=(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
alembic upgrade head; python create_user.py admin devpassword
uvicorn markapp.main:app --reload --port 8011
```
Порт разработки — **8011**, не 8001: на 8001 работает установленная программа
с настоящей базой. Без `MARKING_ONEC_SFTP_HOST` обмен идёт в локальные папки,
к серверу 1С разработка не обращается.

Тесты — `python -m pytest -q` (дважды: в UTC и в поясе Москвы, см. `CLAUDE.md`).
