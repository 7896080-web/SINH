"""Временная страница настройки: Telegram id пользователей, ключ Claude API, токен бота.

Запуск на сервере (через deploy/setup.sh):
    python -m finance.setup_web --env /opt/finance-bot/.env            # только localhost
    python -m finance.setup_web --env ... --public --cert c.pem --key k.pem   # HTTPS наружу

Безопасность:
- адрес содержит одноразовый секрет; без него — 404, после 20 неверных
  попыток страница выключается;
- по умолчанию слушает только 127.0.0.1 (открывать через SSH-туннель);
  публично — только по HTTPS;
- сохранить можно один раз, после этого (или через 15 минут) страница
  выключается сама;
- ключи на странице не показываются целиком; пустое поле — «оставить как есть»;
- .env пишется атомарно с правами 640, остальные строки файла сохраняются.
"""

import argparse
import html
import json
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LIFETIME = 15 * 60
MAX_BAD_ATTEMPTS = 20
FIELDS = ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "ALLOWED_USER_IDS")


# --- .env ------------------------------------------------------------------

def read_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(path: str, updates: dict[str, str], group: str | None = None):
    """Заменить значения ключей, сохранив комментарии и прочие строки."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o640)
    if group and os.geteuid() == 0:
        import grp
        try:
            os.chown(tmp, 0, grp.getgrnam(group).gr_gid)
        except KeyError:
            pass
    os.replace(tmp, path)


def mask(value: str) -> str:
    return "" if not value else ("•••" + value[-4:] if len(value) > 8 else "•••")


# --- проверки ----------------------------------------------------------------

def parse_ids(text: str) -> list[int]:
    ids = []
    for part in text.replace(",", " ").split():
        if not part.isdigit() or not 5 <= len(part) <= 15:
            raise ValueError(f"«{part}» не похоже на Telegram id — это число из 5–15 цифр")
        if int(part) not in ids:
            ids.append(int(part))
    return ids


def telegram_call(token: str, method: str, timeout: float = 10) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read())
        except ValueError:
            return {"ok": False, "error_code": exc.code, "description": str(exc)}


def check_telegram(token: str) -> str:
    """Имя бота (@username) или исключение с понятным текстом."""
    if ":" not in token:
        raise ValueError("токен бота выглядит как 123456789:ABC… — скопируйте его у @BotFather целиком")
    try:
        data = telegram_call(token, "getMe")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError(f"нет связи с Telegram ({exc})") from None
    if not data.get("ok"):
        raise ValueError("Telegram не принял токен — проверьте, что скопировали его целиком")
    return "@" + data["result"]["username"]


def check_anthropic(key: str) -> str:
    """Проверить ключ лёгким запросом к Models API (без расхода токенов)."""
    import anthropic
    try:
        client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=15)
        page = client.models.list(limit=1)
        return f"ключ работает (доступно моделей: {'да' if page.data else 'нет данных'})"
    except anthropic.AuthenticationError:
        raise ValueError("Claude API не принял ключ — проверьте, что скопировали его целиком") from None
    except anthropic.PermissionDeniedError:
        raise ValueError("у ключа нет доступа к API — проверьте права ключа в консоли") from None
    except anthropic.APIConnectionError:
        raise ValueError("нет связи с Claude API") from None
    except anthropic.APIStatusError as exc:
        raise ValueError(f"Claude API ответил ошибкой {exc.status_code}") from None


def recent_senders(token: str) -> list[dict]:
    """Кто писал боту (пока бот не запущен). Список {id, name}."""
    data = telegram_call(token, "getUpdates?limit=100&timeout=0")
    if not data.get("ok"):
        if data.get("error_code") == 409:
            raise ValueError("бот уже запущен и сам забирает сообщения — напишите ему, "
                             "он ответит вашим id")
        raise ValueError("Telegram не принял токен")
    found: dict[int, dict] = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        user = msg.get("from") or (upd.get("callback_query") or {}).get("from")
        if user and not user.get("is_bot"):
            name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
            if user.get("username"):
                name += f" (@{user['username']})"
            found[user["id"]] = {"id": user["id"], "name": name.strip()}
    return list(found.values())


# --- страница ----------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Настройка финансового помощника</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#1c1f24; --muted:#636b76; --line:#dfe3e8;
        --accent:#2f6fde; --ok:#1d7a46; --err:#b3261e; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#15171a; --card:#1e2125; --text:#e8eaed; --muted:#9aa2ad; --line:#30353b;
          --accent:#6e9cf0; --ok:#5cc28a; --err:#f08a80; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
       font:16px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width:560px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.sub {{ color:var(--muted); margin:0 0 20px; font-size:14px; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px; margin-bottom:14px; }}
h2 {{ font-size:16px; margin:0 0 10px; }}
label {{ display:block; font-weight:600; margin:12px 0 4px; font-size:14px; }}
input {{ width:100%; padding:10px 12px; font:inherit; color:var(--text); background:var(--bg);
        border:1px solid var(--line); border-radius:8px; }}
input:focus {{ outline:2px solid var(--accent); border-color:transparent; }}
.hint {{ color:var(--muted); font-size:13px; margin-top:4px; }}
.now {{ color:var(--muted); font-size:13px; }}
button {{ font:inherit; font-weight:600; border:0; border-radius:8px; padding:11px 16px;
         cursor:pointer; background:var(--accent); color:#fff; }}
button.secondary {{ background:transparent; color:var(--accent); border:1px solid var(--accent);
                   padding:8px 12px; font-size:14px; margin-top:10px; }}
.who {{ margin-top:8px; font-size:14px; }}
.who button {{ margin:4px 6px 0 0; padding:6px 10px; font-size:13px; }}
.msg {{ border-radius:8px; padding:10px 12px; margin-bottom:14px; font-size:14px; }}
.msg.err {{ background:color-mix(in srgb, var(--err) 12%, transparent); color:var(--err); }}
.msg.ok {{ background:color-mix(in srgb, var(--ok) 14%, transparent); color:var(--ok); }}
.actions {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
.small {{ font-size:13px; color:var(--muted); }}
</style></head><body><main>
<h1>Настройка финансового помощника</h1>
<p class="sub">Страница временная: закроется после сохранения или через 15 минут.
Пустое поле — оставить текущее значение.</p>
{message}
<form method="post" action="{action}" autocomplete="off">
<input type="hidden" name="csrf" value="{csrf}">
<section><h2>Claude API — общий ключ</h2>
  <label for="key">Ключ API</label>
  <input id="key" name="ANTHROPIC_API_KEY" type="password" placeholder="sk-ant-…"
         spellcheck="false" autocapitalize="off">
  <div class="now">{key_now}</div>
  <div class="hint">platform.claude.com → API Keys → Create Key. Один ключ на обоих.</div>
</section>
<section><h2>Бот в Telegram</h2>
  <label for="tok">Токен бота</label>
  <input id="tok" name="TELEGRAM_BOT_TOKEN" type="password" placeholder="123456789:ABC…"
         spellcheck="false" autocapitalize="off">
  <div class="now">{token_now}</div>
  <div class="hint">У @BotFather: /newbot. Если уже вписан — оставьте пустым.</div>
</section>
<section><h2>Пользователи</h2>
  <label for="u1">Пользователь 1 — Telegram id</label>
  <input id="u1" name="user1" inputmode="numeric" value="{user1}" placeholder="например 123456789">
  <div class="hint">Ему достанутся данные прежней общей базы, если бот уже работал.</div>
  <label for="u2">Пользователь 2 — Telegram id</label>
  <input id="u2" name="user2" inputmode="numeric" value="{user2}" placeholder="например 987654321">
  <div class="hint">У каждого своя база — друг друга данные не видны.
  Как узнать id: каждый пишет боту что угодно и жмёт кнопку ниже.</div>
  <button type="button" class="secondary" id="whoBtn">Кто писал боту</button>
  <div class="who" id="who"></div>
</section>
<div class="actions">
  <button type="submit">Проверить и сохранить</button>
  <span class="small">Токен и ключ проверяются живым запросом.</span>
</div>
</form>
<script>
const who = document.getElementById("who");
document.getElementById("whoBtn").onclick = async () => {{
  who.textContent = "Ищу…";
  const body = new URLSearchParams({{csrf: "{csrf}",
    TELEGRAM_BOT_TOKEN: document.getElementById("tok").value}});
  try {{
    const r = await fetch("{action}/whois", {{method: "POST", body}});
    const data = await r.json();
    who.textContent = "";
    if (data.error) {{ who.textContent = data.error; return; }}
    if (!data.users.length) {{ who.textContent = "Пока никто не писал. Напишите боту «привет» и нажмите ещё раз."; return; }}
    for (const u of data.users) {{
      for (const n of [1, 2]) {{
        const b = document.createElement("button");
        b.type = "button"; b.className = "secondary";
        b.textContent = u.name + " → пользователь " + n;
        b.onclick = () => {{ document.getElementById("u" + n).value = u.id; }};
        who.appendChild(b);
      }}
      who.appendChild(document.createElement("br"));
    }}
  }} catch (e) {{ who.textContent = "Не получилось: " + e; }}
}};
</script>
</main></body></html>"""

DONE_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Готово</title>
<style>body{{font:16px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;max-width:560px;
margin:40px auto;padding:0 16px;background:#f6f7f9;color:#1c1f24}}
@media (prefers-color-scheme: dark){{body{{background:#15171a;color:#e8eaed}}}}</style>
</head><body><h1>✅ Сохранено</h1>{body}
<p>Страница настройки закрыта. Чтобы поменять что-то ещё — запустите её снова:
<code>sudo bash deploy/setup.sh</code></p></body></html>"""


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, env_path: str, secret: str, *, group=None, on_saved=None,
                 checks=True):
        super().__init__(addr, Handler)
        self.env_path = env_path
        self.secret = secret
        self.csrf = secrets.token_urlsafe(16)
        self.group = group
        self.on_saved = on_saved
        self.checks = checks
        self.bad_attempts = 0
        self.saved = False
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: SetupServer

    def log_message(self, fmt, *args):  # без логов с секретом в адресе
        pass

    def _send(self, code: int, body: str, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> str | None:
        """Путь после секрета ('' или '/whois'), None — чужой адрес."""
        path = urlparse(self.path).path
        prefix = "/" + self.server.secret
        if self.server.saved or not (path == prefix or path.startswith(prefix + "/")):
            with self.server.lock:
                self.server.bad_attempts += 1
                if self.server.bad_attempts >= MAX_BAD_ATTEMPTS:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            self._send(404, "Not found", "text/plain")
            return None
        return path[len(prefix):]

    def _form(self) -> dict[str, str]:
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0].strip() for k, v in parse_qs(raw).items()}

    def do_GET(self):
        rest = self._authorized()
        if rest is None:
            return
        if rest == "":
            self._send(200, self.render())
        else:
            self._send(404, "Not found", "text/plain")

    def do_POST(self):
        rest = self._authorized()
        if rest is None:
            return
        form = self._form()
        if not secrets.compare_digest(form.get("csrf", ""), self.server.csrf):
            self._send(403, "Forbidden", "text/plain")
            return
        if rest == "/whois":
            token = form.get("TELEGRAM_BOT_TOKEN") or read_env(self.server.env_path).get(
                "TELEGRAM_BOT_TOKEN", "")
            try:
                if not token:
                    raise ValueError("сначала впишите токен бота")
                result = {"users": recent_senders(token)}
            except (ValueError, OSError) as exc:
                result = {"error": str(exc)}
            self._send(200, json.dumps(result, ensure_ascii=False), "application/json")
            return
        self.save(form)

    def render(self, message: str = "", form: dict | None = None) -> str:
        env = read_env(self.server.env_path)
        ids = [x for x in env.get("ALLOWED_USER_IDS", "").replace(",", " ").split()]
        form = form or {}
        now = lambda key, what: (f"Сейчас: {html.escape(mask(env[key]))}" if env.get(key)
                                 else f"Сейчас: {what} не задан")
        return PAGE.format(
            message=message, action="/" + self.server.secret, csrf=self.server.csrf,
            key_now=now("ANTHROPIC_API_KEY", "ключ"), token_now=now("TELEGRAM_BOT_TOKEN", "токен"),
            user1=html.escape(form.get("user1", ids[0] if ids else "")),
            user2=html.escape(form.get("user2", ids[1] if len(ids) > 1 else "")))

    def save(self, form: dict):
        env = read_env(self.server.env_path)
        updates, errors, notes = {}, [], []
        key = form.get("ANTHROPIC_API_KEY", "")
        token = form.get("TELEGRAM_BOT_TOKEN", "")
        try:
            ids = parse_ids(" ".join(x for x in (form.get("user1", ""), form.get("user2", "")) if x))
            updates["ALLOWED_USER_IDS"] = ",".join(map(str, ids))
            if not ids:
                notes.append("Пользователи не указаны — бот будет отвечать каждому его id.")
        except ValueError as exc:
            errors.append(str(exc))
        if self.server.checks:
            if key:
                try:
                    notes.append("Claude API: " + check_anthropic(key))
                except ValueError as exc:
                    errors.append(str(exc))
            if token:
                try:
                    notes.append("Бот: " + check_telegram(token))
                except ValueError as exc:
                    errors.append(str(exc))
        if key:
            updates["ANTHROPIC_API_KEY"] = key
        if token:
            updates["TELEGRAM_BOT_TOKEN"] = token
        if not (key or env.get("ANTHROPIC_API_KEY")):
            errors.append("нужен ключ Claude API")
        if not (token or env.get("TELEGRAM_BOT_TOKEN")):
            errors.append("нужен токен бота")
        if errors:
            msg = "<div class='msg err'>" + "<br>".join(html.escape(e) for e in errors) + "</div>"
            self._send(200, self.render(msg, form))
            return
        with self.server.lock:
            if self.server.saved:
                self._send(404, "Not found", "text/plain")
                return
            write_env(self.server.env_path, updates, self.server.group)
            self.server.saved = True
        restart = self.server.on_saved() if self.server.on_saved else ""
        body = "".join(f"<p>{html.escape(n)}</p>" for n in notes + ([restart] if restart else []))
        self._send(200, DONE_PAGE.format(body=body))
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def restart_service() -> str:
    try:
        subprocess.run(["systemctl", "restart", "finance-bot"], check=True, timeout=30,
                       capture_output=True)
        time.sleep(3)
        active = subprocess.run(["systemctl", "is-active", "finance-bot"],
                                capture_output=True, text=True).stdout.strip()
        return ("Бот перезапущен и работает." if active == "active"
                else "Бот не запустился — посмотрите: journalctl -u finance-bot -n 50")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Перезапустите бота вручную: sudo systemctl restart finance-bot ({exc})"


def main(argv=None):
    p = argparse.ArgumentParser(description="Временная страница настройки бота")
    p.add_argument("--env", required=True, help="путь к .env")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--public", action="store_true", help="слушать все адреса (нужны --cert/--key)")
    p.add_argument("--cert")
    p.add_argument("--key")
    p.add_argument("--host-hint", default="IP_СЕРВЕРА", help="что показать в ссылке")
    p.add_argument("--group", default="finance-bot", help="группа владельца .env")
    p.add_argument("--no-restart", action="store_true")
    args = p.parse_args(argv)
    if args.public and not (args.cert and args.key):
        sys.exit("--public только по HTTPS: нужны --cert и --key")

    secret = secrets.token_urlsafe(24)
    server = SetupServer(("0.0.0.0" if args.public else "127.0.0.1", args.port), args.env, secret,
                         group=args.group,
                         on_saved=None if args.no_restart else restart_service)
    if args.public:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(args.cert, args.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        url = f"https://{args.host_hint}:{args.port}/{secret}"
    else:
        url = f"http://localhost:{args.port}/{secret}"
    print("\nСтраница настройки открыта на 15 минут. Ссылка (одноразовая):\n")
    print("   " + url + "\n")
    if args.public:
        print("   Браузер предупредит о сертификате — это временный сертификат сервера:\n"
              "   «Дополнительно» → «Перейти на сайт». Соединение при этом шифруется.\n")
    else:
        print("   Открывать через SSH-туннель — на своём компьютере выполните:\n"
              f"   ssh -L {args.port}:localhost:{args.port} root@{args.host_hint}\n"
              "   и откройте ссылку выше в браузере компьютера.\n")
    timer = threading.Timer(LIFETIME, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()
    print("Страница настройки закрыта." + (" Настройки сохранены." if server.saved else ""))


if __name__ == "__main__":
    main()
