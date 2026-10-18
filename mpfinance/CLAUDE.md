# Финансы площадок — инструкции для Claude Code

Отдельная программа (каталог `mpfinance/`, пакет `finapp`, на сервере
`C:\mpfinance`, служба `mpfinance`, порт 8003). Корневой `CLAUDE.md` —
про Sync Admin. Здесь его правила про деньги и 1С действуют так же, но код,
база и накат — свои. Обзор — `README.md` рядом.

## Перед любым изменением

- Тесты — дважды, в UTC и по Москве (сервер в Москве):
  ```bash
  cd mpfinance && pytest -q && TZ=Europe/Moscow pytest -q
  ```
- Изменил `finapp/models.py` → новая миграция (`alembic revision
  --autogenerate`, `DATABASE_URL` на временную SQLite и `MPFIN_SECRETS_KEY`,
  `MPFIN_SESSION_SECRET` в окружении). Проверь файл глазами и прогони
  upgrade → downgrade → upgrade и `alembic check`.
- Календарные даты — `today_local()` / `local_date_of()` /
  `local_day_start_utc()`, не `.date()` от UTC.

## Границы

- **База Sync Admin — только чтение** (`syncdb.py`, `mode=ro`). Ни записи, ни
  импорта его моделей. Схему читаем простым SQL: `barcodes`, `products`,
  `platform_accounts`, `processed_orders`.
- **1С** подключается последним этапом: выключатель на «Диагностике»
  (`settings.ONEC_ENABLED`), по умолчанию выключен. Выключен — ни одного файла
  в `C:\sync`; справочник берётся из базы Sync Admin.
- **1С** (когда включена): свои файлы `task_fin_*`, ответы в `results\finance`.
  - Пока обработка не ответила mark-4 на FIN_PING, не шлём ничего, кроме
    FIN_PING (ответ старой обработки ушёл бы в общую папку sync_admin).
  - Отчёт о продажах уходит только по кнопке и своим файлом.
  - В 1С — баркод, количество, сумма к перечислению.
  - Правка модуля 1С (`1c/` в корне репозитория) — в обоих файлах (.txt и .bsl:
    тот же текст с BOM и CRLF). Корневой `scripts/check_1c_module.py` и тесты
    Sync Admin должны оставаться зелёными: модуль общий.
- **Площадкам — только чтение** (finance-api WB, заказы KIT). Почта — только
  чтение (`readonly`, `BODY.PEEK`).
- **Lamoda**: еженедельный отчёт — предварительный, отчёт комиссионера —
  итоговый. При загрузке отчёта комиссионера предварительные строки за его
  период удаляются (`finance/lamoda.py`).

- **Шкала прогресса** (`static/progress.js`, `finapp/progress.py`). Кнопки
  форм уходят фоном; ответ-переход для них отдаёт JSON (`progress_middleware`
  в `main.py`, стоит ВНУТРИ SessionMiddleware — иначе флеш съедается).
  Формы не отправлять через `form.submit()` (нет события submit) — только
  `requestSubmit()`; ссылки на выгрузки — с `data-download`; долгая операция в
  обработчике сообщает ход через `progress.report`. Новое фоновое дело —
  в `/progress/jobs` (`routers/progress.py`).
- **AWER CRM**: «Сумма» — до комиссии (ставка — настройка). Продажи KIT из CRM
  → загрузка KIT по API берёт только доставку (`fetch._run_kit`).

## Накат на сервер

Пакет: `python scripts/gen_package.py --tag finN --out mpfinance_finN.ps1`.
Его запускают на сервере, затем `deploy\update_server.ps1 -Tag finN`. Первая
установка — `deploy\install_server.ps1`. Подтверждение человека нужно перед
правкой `.env` на сервере и перед восстановлением базы из копии.
