# Развёртывание на Windows Server (локальный вариант)

Целевой сервер — Windows Server `136.243.92.95` (WIN-Q0QVP2SM59L), где уже
работает 1С. Приложение и планировщик крутятся здесь же, обмен с 1С — через
локальную папку `C:\sync` (без FTP).

## Предпосылки (поставить заранее)

| Компонент | Зачем | Обязателен |
|---|---|---|
| **Python 3.11–3.14** | сам runtime (при установке — галочка «Add to PATH») | да |
| **PostgreSQL для Windows** | боевая БД (веб + планировщик — два процесса) | рекомендуется |
| **NSSM** (`nssm.exe` в PATH) | запуск веба и планировщика как служб Windows | для автозапуска |

Без PostgreSQL скрипт откатится на SQLite-файл — для небольшого объёма приемлемо,
но при активной одновременной записи двух процессов PostgreSQL надёжнее.
Без NSSM всё установится, но службы придётся поднять вручную (скрипт покажет как).

## Запуск

PowerShell **от имени администратора**:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_windows.ps1
```

Параметры (необязательно): `-SyncRoot C:\sync` (папка обмена), `-WebPort 8000`.

## Что делает скрипт

1. Проверяет Python 3.11+.
2. Создаёт `.venv` и ставит зависимости.
3. Создаёт `C:\sync\{tasks,results,archive}`.
4. Генерирует `.env` (если его ещё нет): `SESSION_SECRET`, `SECRETS_ENCRYPTION_KEY`
   (Fernet), `DATABASE_URL`, `SYNC_DIR_*`. Секреты в консоль не выводятся.
5. Создаёт роль/БД PostgreSQL (если доступен `psql` и введён пароль postgres).
6. Прогоняет миграции `alembic upgrade head`.
7. Создаёт пользователя `admin` со случайным паролем (**показывается один раз**),
   если пользователей ещё нет.
8. Регистрирует и запускает службы `sync_admin_web` (uvicorn) и
   `sync_admin_worker` (планировщик) через NSSM.

Скрипт идемпотентен: существующий `.env` и пользователь-админ не пересоздаются.

## После установки

- Веб-админка: `http://127.0.0.1:8000/login` (логин/пароль из вывода скрипта).
- Здоровье воркеров: `http://127.0.0.1:8000/health`.
- Логи служб: `logs\<имя службы>.log` (stdout) и `logs\<имя службы>.err.log` (stderr).
  Имена задаются при установке, и на сервере, развёрнутом не этим скриптом, они
  другие — на боевом это `logs\web.err.log` и `logs\worker.err.log`. Точный путь
  всегда можно спросить у самой службы: `nssm get sync_admin_worker AppStderr`.
  Всё содержательное (Python logging) идёт в **err**-файл; `.log` обычно пустой.
- Время в логе — **местное**, со смещением в конце (`16:00:20,561+0300`), а в базе
  и на страницах админки — UTC. Не сопоставляйте их напрямую: три часа разницы
  один раз уже дали ложный вывод «1С обработала задание дважды».
- Читать лог под Windows PowerShell — только с явной кодировкой, иначе кириллица
  не найдётся: `Select-String -Path ... -Encoding UTF8`.

Дальнейшие шаги в самой админке:
1. Ввести API-ключи площадок (страница «API-ключи»).
2. Заполнить `WAREHOUSE_ID_WB/OZON/KIT` в `.env` (ID склада продавца на площадке),
   перезапустить службу `sync_admin_worker`.
3. Убедиться, что 1С-обработка `ОбменССайтом.epf` смотрит в те же
   `C:\sync\{tasks,results,archive}` и стоит на расписании.

## Управление службами

```powershell
nssm restart sync_admin_worker      # перечитать .env после правок
nssm stop sync_admin_web
nssm status sync_admin_worker
Get-Content (nssm get sync_admin_worker AppStderr) -Tail 50
```

Размер и время файла в `Get-ChildItem` у работающей службы **отстают**: пока NSSM
держит файл открытым, Windows обновляет эти поля лениво. Судить о том, пишется ли
лог, по ним нельзя — читайте хвост самого файла.

### Ротация лога

NSSM по умолчанию пишет в один файл без ограничения размера — за месяцы это
десятки мегабайт, и открыть их в редакторе уже нечем. Включается так (раз и
навсегда, служба переживает обновления):

```powershell
nssm set sync_admin_worker AppRotateFiles 1
nssm set sync_admin_worker AppRotateOnline 1
nssm set sync_admin_worker AppRotateBytes 10485760   # 10 МБ
nssm set sync_admin_web    AppRotateFiles 1
nssm set sync_admin_web    AppRotateOnline 1
nssm set sync_admin_web    AppRotateBytes 10485760
nssm restart sync_admin_worker
nssm restart sync_admin_web
```

## Доступ снаружи (если нужен)

По умолчанию веб слушает `127.0.0.1` — только локально. Чтобы открыть админку
из сети, поставьте перед приложением обратный прокси с HTTPS (IIS ARR или Nginx),
проксируйте на `127.0.0.1:8000` и выставьте в `.env` `SESSION_COOKIE_SECURE=1`.
Публиковать голый HTTP-порт наружу не нужно.

## Обновление кода

```powershell
git pull                                   # или скопировать новые файлы
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
nssm restart sync_admin_web
nssm restart sync_admin_worker
```
