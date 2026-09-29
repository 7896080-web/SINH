# Установка и обновление программы «Маркировка и поставки» (Windows Server)

Программа ставится **рядом** с sync_admin на тот же сервер (обмен с 1С идёт
через локальные папки) и **не трогает** его: свой каталог `C:\marking`,
свои службы `marking_web` / `marking_worker`, своя база, свой `.env`.

## Первая установка

1. **Файлы.** На сервере создайте `C:\marking` и скопируйте туда содержимое
   каталога `marking/` репозитория (ветка с программой). Должно получиться
   `C:\marking\markapp\main.py`, `C:\marking\deploy\install_marking.ps1` и т. д.
2. **Установка** (PowerShell от администратора):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\marking\deploy\install_marking.ps1
   ```
   Скрипт:
   - находит Python (тот же, что у sync_admin) и создаёт свой venv;
   - создаёт `.env` со **своими** секретами;
   - создаёт папки `C:\sync\results\marking` и `C:\sync\archive\marking`;
   - прогоняет миграции и тесты;
   - спрашивает логин и пароль первого пользователя;
   - ставит службы и ждёт зелёный `/health`.
3. **Ключ шифрования.** Откройте `C:\marking\.env` и перенесите
   `MARKING_SECRETS_KEY` туда, где храните пароли. **Не** на Яндекс.Диск рядом
   с копиями: копия базы вместе с ключом — это утечка.
4. **Обработка 1С** — по `C:\marking\1c\ОБНОВЛЕНИЕ_ОБРАБОТКИ.md`, с проверкой
   на копии базы. Затем в программе: «Диагностика» → «Отправить PING».
   Пока ответа нет, программа задания поставок в 1С не шлёт.
5. **Справочник и организация.** «Одежда полный» → загрузить выгрузку каталога
   Lamoda Seller. «Организации» → проверить реквизиты ИП Яворской и номер
   последней поставки (12550).

Открывается программа в браузере на сервере по адресу `http://127.0.0.1:8001`.
Снаружи её нет: служба слушает только `127.0.0.1`. Путь с машины с КриптоПро
(понадобится с этапа 4) — SSH-туннель, `SSH_TUNNEL.md`.

## Обновление

1. Остановки не нужно. Скопируйте новые файлы поверх `C:\marking` (кроме
   `.env`, `marking.db`, `backups\`, `logs\`, `tools\`, `rclone.conf`).
2. Запустите:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\marking\deploy\update_marking.ps1
   ```
   Скрипт по порядку: копия базы → зависимости → миграции → тесты →
   перезапуск служб → опрос `/health` до 150 с. Любой сбой — остановка с
   кодом 1.

## Копия базы в облако (Яндекс.Диск)

У программы свой rclone и свой конфиг. Общий конфиг с sync_admin не
годится: rclone переписывает его при обновлении токена, и две программы
могут испортить файл разом.

1. Положите `rclone.exe` в `C:\marking\tools\`.
2. Настройте доступ к облаку:
   ```powershell
   C:\marking\tools\rclone.exe --config C:\marking\rclone.conf config
   ```
   Выберите: `n` → имя `yandex` → **Yandex Disk** → `client_id` и
   `client_secret` пустые → авторизация. Если браузер на сервере не
   открывается, выполните `rclone authorize "yandex"` на своём компьютере и
   вставьте полученный токен.
3. Создайте папку в облаке:
   ```powershell
   C:\marking\tools\rclone.exe --config C:\marking\rclone.conf mkdir yandex:marking_backups
   ```
4. В `C:\marking\.env` пропишите `MARKING_RCLONE_REMOTE=yandex:marking_backups`
   и выполните `nssm restart marking_worker`.

Суточная копия снимается через 10 минут после старта воркера, потом раз в
сутки. Уезжает в облако, после чего программа сверяет размер на той стороне.
Состояние видно на «Диагностике».

## Восстановление из копии

```powershell
nssm stop marking_web
nssm stop marking_worker
# Прежнюю базу вместе со спутниками -wal/-shm отложить, а не удалять:
Rename-Item C:\marking\marking.db marking.db.before-restore
Remove-Item C:\marking\marking.db-wal, C:\marking\marking.db-shm -ErrorAction SilentlyContinue
Copy-Item C:\marking\backups\marking-ГГГГММДД-ЧЧММСС.db C:\marking\marking.db
nssm start marking_worker
nssm start marking_web
```
`-wal` и `-shm` удалять **обязательно**: иначе SQLite при первом открытии
накатит их поверх копии со страницами прежней базы.
