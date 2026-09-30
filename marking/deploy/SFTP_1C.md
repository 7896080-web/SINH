# Обмен с 1С по SFTP: рабочий компьютер с КриптоПро → сервер

Программа стоит на рабочем компьютере с КриптоПро, а 1С и её обработка
`ОбменССайтом` — на сервере. Протокол обмена не меняется: задание ложится в
`C:\sync\tasks`, ответ приходит в `C:\sync\results\marking`. Меняется только
путь файла — SFTP до сервера на порт 443. На сервере для программы до этого
не установлено НИЧЕГО; ставится один OpenSSH Server (Microsoft) и его
настройка. Почему так, а не FTP, — `ТЗ_МАРКИРОВКА.md`, п. 9.1.

```
программа (рабочий компьютер) ── SFTP, TCP 443 ──► сервер 136.243.92.95
    кладёт  /tasks/task_mark_*.txt         = C:\sync\tasks
    читает  /results/marking/result_mark_* = C:\sync\results\marking
    уносит  в /archive/marking             = C:\sync\archive\marking
```

**Правила:**
- **Закрытый ключ не покидает рабочий компьютер** (`C:\marking\ssh\id_ed25519`).
  На сервер попадает только строка из `id_ed25519.pub`. В чат и в репозиторий
  не отправлять ни то, ни другое.
- Учётная запись `marking_sftp` заперта в `C:\sync` и умеет **только SFTP**:
  ни командной строки, ни проброса портов, ни RDP.
- Паролей у SSH нет, вход только по ключу.
- Ключ сервера сверяется **глазами** один раз (шаг 5). Программа потом
  откажется работать с сервером, чей ключ изменился, — это не поломка, а
  защита от подмены: наши задания двигают товар в 1С.

Команды на сервере — PowerShell **от администратора**.

## Коротко: что ставится на сервер

**Только OpenSSH Server** (бесплатный, от Microsoft) и его настройка. Ни
Python, ни самой программы, ни служб маркировки на сервере нет. Отдельно, в
Конфигураторе, обновляется обработка 1С (`1c/ОБНОВЛЕНИЕ_ОБРАБОТКИ.md`).

1. Если OpenSSH Server ещё не стоит — скачать `OpenSSH-Win64-v*.msi` (не
   Preview) с `github.com/PowerShell/Win32-OpenSSH/releases` и:
   ```powershell
   msiexec /i C:\путь\OpenSSH-Win64-vX.X.X.X.msi ADDLOCAL=Server
   ```
2. Скопировать на сервер `server_sftp_setup.ps1` (например, в
   `C:\marking_setup\`) и запустить с открытым ключом рабочего компьютера
   (строка из вывода `install_workstation.ps1`):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\marking_setup\server_sftp_setup.ps1 -PublicKey "ssh-ed25519 AAAA... marking@PC"
   ```
   Скрипт делает шаги 2–5 ниже сам: учётная запись, папки и права,
   `sshd_config` (с копией прежнего; при ошибке проверки возвращает прежний),
   ключ, брандмауэр, служба — и печатает отпечаток ключа сервера для сверки.
   Повторный запуск безопасен.

Шаги ниже — то же самое руками, для справки и разбора.

---

## 1. Сервер: OpenSSH на 443

1. Скачать `OpenSSH-Win64-vX.X.X.X.msi` (последний не `Preview`) со страницы
   `github.com/PowerShell/Win32-OpenSSH/releases` и поставить только сервер:
   ```powershell
   msiexec /i C:\путь\OpenSSH-Win64-vX.X.X.X.msi ADDLOCAL=Server
   ```
2. Запустить один раз — служба создаст `C:\ProgramData\ssh\sshd_config` и
   ключи сервера — и остановить:
   ```powershell
   Start-Service sshd
   Stop-Service sshd
   ```
3. Проверить, что порт 443 никем не занят (пустой вывод — свободен):
   ```powershell
   netstat -ano | findstr ":443 " | findstr LISTENING
   ```
   Занят (IIS, другая программа) — остановиться и разобраться: SSH на него
   не встанет. Скрипт `server_sftp_setup.ps1` проверяет это сам.
4. В `C:\ProgramData\ssh\sshd_config` **в начале файла** (до первой строки
   `Match`): `#Port 22` → `Port 443`, `#PasswordAuthentication yes` →
   `PasswordAuthentication no`. Правило брандмауэра установщика на 22
   выключить — SSH там больше не слушает:
   ```powershell
   Get-NetFirewallRule | Where-Object { $_.DisplayName -like "*OpenSSH*" } | Disable-NetFirewallRule
   New-NetFirewallRule -DisplayName "SFTP marking 443" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
   Set-Service sshd -StartupType Automatic
   ```

## 2. Сервер: учётная запись и папки

```powershell
$pw = Read-Host -AsSecureString "Пароль для marking_sftp (Windows требует; для SSH не используется)"
New-LocalUser -Name marking_sftp -Password $pw -PasswordNeverExpires -UserMayNotChangePassword -Description "SFTP обмена маркировки с 1С"

New-Item -ItemType Directory -Force C:\sync\results\marking, C:\sync\archive\marking | Out-Null
icacls C:\sync\tasks            /grant "marking_sftp:(OI)(CI)M"
icacls C:\sync\results\marking  /grant "marking_sftp:(OI)(CI)M"
icacls C:\sync\archive\marking  /grant "marking_sftp:(OI)(CI)M"
```

В группы «Администраторы» и «Пользователи удалённого рабочего стола» её
**не добавлять**. Писать ей можно только в эти три папки.

## 3. Сервер: настройки SSH

```powershell
notepad C:\ProgramData\ssh\sshd_config
```

В начале файла (до первой строки `Match`) добавить строку
`AllowUsers marking_sftp`. С ней SSH на этом сервере пускает только эту
учётную запись.

**В самый конец файла** дописать:
```
Match User marking_sftp
    AuthorizedKeysFile __PROGRAMDATA__/ssh/marking_sftp_keys
    ChrootDirectory C:\sync
    ForceCommand internal-sftp
    AllowTcpForwarding no
    PermitTunnel no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
```

Проверить (пустой вывод — всё в порядке):
```powershell
& "C:\Program Files\OpenSSH\sshd.exe" -t
```

## 4. Рабочий компьютер: установка программы и ключ

Скопировать содержимое каталога `marking/` репозитория в `C:\marking` и
запустить (права администратора не нужны):
```powershell
powershell -ExecutionPolicy Bypass -File C:\marking\deploy\install_workstation.ps1 -Server 136.243.92.95
```
Установщик создаст ключ и напечатает строку `ssh-ed25519 …` — её на сервер:
```powershell
notepad C:\ProgramData\ssh\marking_sftp_keys
```
Вставить строку, сохранить, права — только Администраторы и SYSTEM (иначе
SSH файл проигнорирует), перезапустить службу:
```powershell
icacls C:\ProgramData\ssh\marking_sftp_keys /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F"
Restart-Service sshd
```

## 5. Сверить ключ сервера

Установщик записал ключ сервера в `C:\marking\ssh\known_hosts` и напечатал
отпечаток вида `256 SHA256:AbCd… [136.243.92.95]:443 (ED25519)`. На **сервере**:
```powershell
& "C:\Program Files\OpenSSH\ssh-keygen.exe" -l -f C:\ProgramData\ssh\ssh_host_ed25519_key.pub
```
Строки `SHA256:…` должны совпасть **посимвольно**. Не совпали — удалить
`C:\marking\ssh\known_hosts` и разбираться: вы говорите не с тем сервером.

## 6. Проверка

На рабочем компьютере:
```powershell
sftp -P 443 -o KexAlgorithms=curve25519-sha256 -i C:\marking\ssh\id_ed25519 -o UserKnownHostsFile=C:\marking\ssh\known_hosts marking_sftp@136.243.92.95
```
В приглашении `sftp>`: `ls` показывает `tasks`, `results`, `archive`
(и другое содержимое `C:\sync`) — `bye`. `KexAlgorithms` нужен встроенному в
Windows клиенту 9.5: без него он не договаривается с OpenSSH 10 на сервере
(«unsupported KEX method»). Программе он не нужен — она ходит через paramiko.

Лишнего не открыто:
- `ssh -p 443 -o KexAlgorithms=curve25519-sha256 -i C:\marking\ssh\id_ed25519 -o UserKnownHostsFile=C:\marking\ssh\known_hosts marking_sftp@136.243.92.95` —
  «This service allows sftp connections only», командной строки нет.

В программе: ярлык «Маркировка» → «Диагностика» → «Отправить PING». Ответ
придёт после ближайшего запуска обработки 1С по расписанию (обычно в пределах
минуты-двух). Пока ответа нет, задания поставок программа в 1С не шлёт.

## Что если…

- **«нет файла ключей сервера»** на «Диагностике» — не пройден шаг 5
  (установщик не достучался до сервера). Запустите установщик снова.
- **«КЛЮЧ СЕРВЕРА ИЗМЕНИЛСЯ»** — SSH на сервере переустанавливали, или это
  подмена. Сначала выяснить, потом удалить `known_hosts` и повторить шаг 5.
- **«недоступен»** — сервер или интернет. Задания подождут: программа
  повторит отправку в следующий проход, ничего не теряется.
