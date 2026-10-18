# Резервные копии: вторая площадка в Selectel S3

Состояние на 16.10.2026. Читать перед любой правкой бэкапов, путей данных,
расписаний Windows или `rclone.conf` любой программы на сервере.

Почему так: с 10.10.2026 Яндекс.Диск недоступен (атаки на дата-центры Яндекса
7–11.10, Yandex Cloud в аварийном режиме), облачные копии не доезжали. 15–16.10
вторую площадку для всех программ перевели на Selectel S3.

## Хранилище

| Что | Значение |
| --- | --- |
| Провайдер | Selectel, объектное хранилище S3 |
| Бакет | `awer-backups`, приватный, класс «стандартный», адресация vHosted, без версионирования и Object Lock |
| Пул | Санкт-Петербург, `ru-1`, адрес `s3.ru-1.storage.selcloud.ru` |
| Доступ | сервисный пользователь `backup-bot`, роль `s3.admin` в проекте My First Project; один S3-ключ |
| rclone | `C:\sync_admin\tools\rclone.exe` (≥ 1.69, `provider=Selectel`); у КАЖДОЙ программы свой `rclone.conf`, remote называется `selectel` |

Ключи лежат только в `rclone.conf` на машинах. В репозиторий, в облако и в чат их не класть.

## Что копируется

| Программа | Где | Что уходит в облако | Путь в облаке | Как часто | Локально | В облаке | Состояние |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Sync Admin | сервер, `C:\sync_admin` | `sync_admin.db` (≈330 МБ) | `selectel:awer-backups/sync_admin` | раз в сутки | 14 ежедн. + 8 недельн. | 30 ежедн. + 12 недельн. | работает с 15.10 |
| Репрайсер | сервер, `C:\repricer` | `repricer-*.db` (≈90 МБ) | `selectel:awer-backups/repricer` | каждые 6 ч | всё за сутки + 1/день за 14 дней | 30 дней | работает с 15.10 |
| Финпомощник | сервер, `C:\FinanceBot` | `finance-*.db`, `receipts-*.zip`, `receipts-*/` | `selectel:awer-backups/financebot` | 04:30 | 30 дней | 30 дней; `receipts-*/` без срока | работает с 15.10 |
| AWER CRM | сервер, `C:\awer_crm_data` | снимок `kitcrm.db` (SQLite backup + integrity_check); разово 5 копий из `backups\` | `selectel:awer-backups/awer_crm` (+ `/app_backups`) | каждые 6 ч | 7 снимков | 30 дней; `app_backups` без срока | работает с 15.10 |
| direct (WB, Директ) | сервер, `C:\direct` | zip: снимки `wb_history.db`, `wb_rec.db` + таблицы, откаты, журналы, скрипты | `selectel:awer-backups/direct` | 05:30 | 7 архивов | 30 дней | работает с 16.10 |
| Маркировка | рабочий ПК, `C:\marking` | `marking.db` | `selectel:awer-backups/marking` | раз в сутки, через 10 мин после запуска | по программе | по программе | **НЕ настроено**: в `.env` ещё `yandex:marking_backups` |

Время в именах копий Sync Admin — UTC, у остальных — московское.

## Чем управляется

| Программа | Механизм | Конфиг rclone | Журнал | В облако НЕ попадает |
| --- | --- | --- | --- | --- |
| Sync Admin | встроено (`app/backup.py`); поле «Путь в облаке» на странице «Уведомления» = `selectel:awer-backups/sync_admin` | `C:\sync_admin\rclone.conf` | «Диагностика» → задание `backup`; `scripts\probe_backup_mirror.py` | `.env` (`SECRETS_ENCRYPTION_KEY`) |
| Репрайсер | задача Windows «Repricer cloud backup» (SYSTEM), `C:\repricer\tools\cloud_copy.ps1` | `C:\repricer\rclone.conf` | `C:\repricer\logs\cloud_copy.log` | `.env` |
| Финпомощник | задача «FinanceBot cloud backup» (SYSTEM), `C:\FinanceBot\tools\cloud_copy.ps1` | `C:\FinanceBot\rclone.conf` | `C:\FinanceBot\logs\cloud_copy.log` | `env-latest.txt` |
| AWER CRM | задача «AWER CRM cloud backup» (SYSTEM), `C:\awer_crm_data\cloud_backup\cloud_copy.ps1` + `snap.py` | `C:\awer_crm_data\cloud_backup\rclone.conf` | `C:\awer_crm_data\cloud_backup\cloud_copy.log` | `config.json`, `auth.json` |
| direct | задача «Direct cloud backup» (SYSTEM), `C:\direct_backup\cloud_copy.ps1` + `snap_direct.py` | `C:\direct_backup\rclone.conf` | `C:\direct_backup\cloud_copy.log` | `wb_tokens.txt`, `*token*.txt`, `config.json`, `crm_config.json`, `.env`, `.venv`, `*.old.*` |
| Маркировка | встроено; `MARKING_RCLONE_REMOTE` в `C:\marking\.env` | `C:\marking\rclone.conf` | «Диагностика» Маркировки | `.env` |

Скрипты задач Windows написаны в ASCII нарочно: Windows PowerShell на сервере
портит кириллицу в файлах без BOM. Вывод Python в журнал — через
`Out-File -Encoding ascii`, не `*>>` (тот пишет UTF-16 вперемешку с UTF-8 rclone).

## Правила для правок

- Меняешь путь данных программы, имя её базы или каталог копий — поправь её задачу/скрипт выгрузки и эту таблицу в том же коммите.
- Новая программа с базой на сервере — сразу добавь её сюда и на вторую площадку.
- Живую SQLite-базу в облако копировать только через снимок (`Connection.backup()` + `PRAGMA integrity_check`), не файловой копией.
- Файлы с токенами и ключами в облако не уходят никогда; новый секретный файл — добавь в исключения скрипта.
- Задачи выгрузки только КОПИРУЮТ и удаляют в облаке файлы по своей маске старше срока. Не менять на `rclone sync`: он удалит в облаке всё, чего нет локально.

## Осталось сделать

- awer-sales-bot (`awer-sales-bot/`, база `data/awer.db`) ещё не установлен. При установке — снимок базы в `selectel:awer-backups/awer_sales_bot` по образцу Финпомощника, `.env` и `config.yaml` в исключения; добавить строку в таблицы выше.
- Маркировка: `rclone config create selectel …` с `--config C:\marking\rclone.conf`, затем `MARKING_RCLONE_REMOTE=selectel:awer-backups/marking` в `.env` и перезапуск.
- Защита от удаления: отдельный бакет с Object Lock или ключ без права удаления, чтобы рабочий ключ не мог стереть все копии.
- Встроить выгрузку в облако в Репрайсер, Финпомощник и CRM (как в Sync), чтобы её состояние было на «Диагностике», а не только в журнале.
- Раз в месяц: скачать одну копию из облака и открыть её.
- Следить за балансом Selectel: при нуле доступ закрывается, через 30 дней данные удаляются.
- Когда оживёт Яндекс.Диск — решить, держать ли его третьей площадкой (сейчас поле Sync принимает одно облако).
