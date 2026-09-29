# SSH-туннель: машина с КриптоПро → программа на сервере

> **Не выполнялся и не нужен.** С 29.09.2026 программа стоит на рабочем
> компьютере с КриптоПро, до сервера она ходит только по SFTP к папкам 1С —
> всё про сервер теперь в `SFTP_1C.md`. Файл оставлен как история решения.

Зачем и почему именно так — `ТЗ_МАРКИРОВКА.md`, п. 10.2. Коротко: WireGuard
с сети машины с КриптоПро режется провайдером, а TCP 443 до сервера
проходит (проверено 29.09.2026).

```
браузер (машина с КриптоПро) → http://localhost:8001
   → ssh, TCP 443 → сервер 136.243.92.95 → 127.0.0.1:8001 (программа)
```

**Правила:**
- **Закрытый ключ не покидает машину с КриптоПро.** На сервер попадает
  только открытая часть (строка из файла `.pub`). В чат и в репозиторий
  не отправлять ни то, ни другое.
- Через этот вход можно попасть ТОЛЬКО на порт 8001 сервера: ни RDP, ни
  sync_admin, ни командной строки (`PermitOpen`, `ForceCommand`).
- Паролей у SSH нет вовсе, вход только по ключу.

Все команды — PowerShell **от администратора**.

---

## 1. Сервер: установить OpenSSH Server

1. Скачать `OpenSSH-Win64-vX.X.X.X.msi` со страницы релизов Microsoft
   `github.com/PowerShell/Win32-OpenSSH/releases` (последний не `Preview`).
2. Установить только сервер:
   ```powershell
   msiexec /i C:\путь\OpenSSH-Win64-vX.X.X.X.msi ADDLOCAL=Server
   ```
3. Запустить один раз — служба создаст файл настроек
   `C:\ProgramData\ssh\sshd_config` и ключи сервера — и остановить:
   ```powershell
   Start-Service sshd
   Stop-Service sshd
   ```

## 2. Сервер: учётная запись только для туннеля

```powershell
$pw = Read-Host -AsSecureString "Пароль для marking_tunnel (Windows требует; для SSH не используется)"
New-LocalUser -Name marking_tunnel -Password $pw -PasswordNeverExpires -UserMayNotChangePassword -Description "SSH-туннель программы маркировки"
```

В группы «Администраторы» и «Пользователи удалённого рабочего стола» её
**не добавлять**.

## 3. Сервер: настройки SSH

Открыть в Блокноте `C:\ProgramData\ssh\sshd_config`:
```powershell
notepad C:\ProgramData\ssh\sshd_config
```

**В начале файла** (до любой строки `Match`):
- строку `#Port 22` заменить на `Port 443`;
- строку `#PasswordAuthentication yes` заменить на `PasswordAuthentication no`;
- добавить строку `AllowUsers marking_tunnel`.

**В самый конец файла** дописать:
```
Match User marking_tunnel
    AuthorizedKeysFile __PROGRAMDATA__/ssh/marking_tunnel_keys
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:8001
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    ForceCommand cmd.exe /c exit
```

Сохранить. Проверить, что в файле нет ошибок (пустой вывод — всё в
порядке):
```powershell
& "C:\Program Files\OpenSSH\sshd.exe" -t
```

## 4. Машина с КриптоПро: ключ

```powershell
ssh -V
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\marking_tunnel"
```
На вопрос о парольной фразе — дважды Enter (пустая): туннель должен
подниматься сам, без человека. Ключом при этом можно только пробросить
порт 8001, больше ничего.

Показать открытую часть и скопировать строку целиком (начинается с
`ssh-ed25519`):
```powershell
Get-Content "$env:USERPROFILE\.ssh\marking_tunnel.pub"
```

## 5. Сервер: положить открытый ключ, открыть порт, включить службу

```powershell
notepad C:\ProgramData\ssh\marking_tunnel_keys
```
Вставить строку `ssh-ed25519 …` из шага 4, сохранить. Затем права на файл
(иначе SSH его проигнорирует) — только Администраторы и SYSTEM:
```powershell
icacls C:\ProgramData\ssh\marking_tunnel_keys /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F"
```

Порт и служба:
```powershell
New-NetFirewallRule -DisplayName "SSH tunnel marking 443" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
Get-NetFirewallRule | Where-Object { $_.DisplayName -like "*OpenSSH*" } | Disable-NetFirewallRule
Set-Service sshd -StartupType Automatic
Start-Service sshd
netstat -ano | findstr :443
```
Последняя команда должна показать `0.0.0.0:443 ... LISTENING`. Правило
установщика на порт 22 выключено второй командой: SSH на 22 больше не
слушает, открытым его держать незачем.

## 6. Проверка

**Сервер** — временная проверочная страница на `127.0.0.1:8001` (окно не
закрывать):
```powershell
C:\sync_admin\.venv\Scripts\python.exe -m http.server 8001 --bind 127.0.0.1 --directory C:\marking\tools
```

**Машина с КриптоПро** — поднять туннель вручную:
```powershell
ssh -N -L 8001:127.0.0.1:8001 -p 443 -i "$env:USERPROFILE\.ssh\marking_tunnel" -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes marking_tunnel@136.243.92.95
```
В первый раз SSH спросит, доверять ли ключу сервера, — ответить `yes`.
Дальше команда «висит» без вывода: так и должно быть, туннель работает,
пока открыто окно.

В браузере на этой же машине: `http://localhost:8001/plugin_check.html`.
Что смотреть — в самой странице и в ТЗ, п. 10.2.

Проверить, что ничего лишнего не открыто (оба должны быть отказом):
- в браузере `http://136.243.92.95:8001` — не открывается;
- `ssh -p 443 -i "$env:USERPROFILE\.ssh\marking_tunnel" marking_tunnel@136.243.92.95`
  — соединение сразу закрывается, командной строки нет.

## 7. Машина с КриптоПро: туннель сам при входе в Windows

Файл `C:\marking\tunnel.ps1` (создать папку, если нет):
```powershell
while ($true) {
    & ssh.exe -N -L 8001:127.0.0.1:8001 -p 443 `
        -i "$env:USERPROFILE\.ssh\marking_tunnel" `
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
        -o ExitOnForwardFailure=yes -o BatchMode=yes `
        marking_tunnel@136.243.92.95
    Start-Sleep -Seconds 10
}
```
Задание планировщика от ТЕКУЩЕГО пользователя (того, под кем открыт
браузер с КриптоПро):
```powershell
schtasks /create /tn "marking-tunnel" /sc onlogon /rl limited /tr "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\marking\tunnel.ps1"
schtasks /run /tn "marking-tunnel"
```
Оборвалась связь — через 10 секунд туннель поднимется снова.

## 8. Убрать WireGuard

Он с этой сети не работает, а открытый неработающий путь держать незачем.

**Сервер:**
```powershell
Remove-NetFirewallRule -DisplayName "WireGuard marking"
Remove-NetFirewallRule -DisplayName "marking 8001 (VPN)"
```
и в WireGuard: туннель `marking` → «Отключить» → «Удалить».

**Машина с КриптоПро:** в WireGuard туннель `marking` → «Отключить» →
«Удалить».
