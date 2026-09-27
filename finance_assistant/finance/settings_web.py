"""Постоянная страница настроек под паролем (служба finance-bot-settings).

    python -m finance.settings_web set-password --dir /opt/finance-bot/settings
    python -m finance.settings_web serve --env /opt/finance-bot/.env \\
        --dir /opt/finance-bot/settings --port 8765

Что на странице — то же, что на одноразовой (setup_web): ключ Claude API,
токен бота, Telegram id двух пользователей. Отличия — вход по паролю,
который вы задаёте сами, и страница работает всегда.

Защита:
- только HTTPS (сертификат сервера из --dir);
- пароль хранится хешем scrypt; без заданного пароля служба не стартует;
- 5 неверных паролей с одного адреса — адрес заблокирован на 15 минут;
  20 неверных за час с любых адресов — вход закрыт для всех на час;
  каждая попытка — в журнале службы;
- сессия: случайный токен в cookie (Secure, HttpOnly, SameSite=Strict),
  30 минут без действий или 12 часов всего; кнопка «Выйти»;
- каждая форма — с CSRF-токеном сессии; CSP запрещает чужие скрипты и фреймы;
- ключи целиком не показываются; служба меняет только файл .env,
  бот сам замечает изменение и перезапускается.
"""

import argparse
import getpass
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import ssl
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import setup_web
from .setup_web import PAGE, mask, parse_ids, read_env, write_env

log = logging.getLogger("finance.settings")

MIN_PASSWORD = 10
IP_FAILS, IP_LOCK = 5, 15 * 60          # 5 ошибок с адреса → 15 минут блокировки
GLOBAL_FAILS, GLOBAL_WINDOW = 20, 3600  # 20 ошибок за час → вход закрыт на час
TRUST_DAYS = 30 * 86400                  # адрес с верным входом не попадает под общую блокировку
IDLE, ABSOLUTE = 30 * 60, 12 * 3600     # сессия: без действий / всего
COOKIE = "fb_session"
PASSWORD_FILE = "password"


# --- пароль ------------------------------------------------------------------

def hash_password(password: str, *, n: int = 2 ** 15) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, maxmem=128 * 2 ** 20)
    return f"scrypt${n}$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.strip().split("$")
        if algo != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n),
                                   r=int(r), p=int(p), maxmem=128 * 2 ** 20)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def check_new_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"пароль короче {MIN_PASSWORD} символов"
    if password.isdigit() or len(set(password)) < 5:
        return "слишком простой пароль: добавьте буквы и разные символы"
    return None


def save_password_file(folder: str, password: str):
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = os.path.join(folder, PASSWORD_FILE)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(hash_password(password) + "\n")
    os.replace(path + ".tmp", path)


# --- ограничение попыток и сессии ---------------------------------------------

class Guard:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.ip_fails: dict[str, list[float]] = {}
        self.ip_locked: dict[str, float] = {}
        self.global_fails: list[float] = []
        self.global_locked_until = 0.0
        self.sessions: dict[str, dict] = {}
        self.inflight: set[str] = set()      # адреса, чей пароль проверяется прямо сейчас
        self.trusted: dict[str, float] = {}  # адрес → когда с него входили верно

    def begin_attempt(self, ip: str) -> str | None:
        """Занять попытку входа ДО проверки пароля. Попытка сразу считается
        неверной (снимается при успехе): параллельные запросы не обходят лимит.
        Проверка пароля (scrypt, ~32 МБ памяти) — не больше двух одновременно."""
        blocked = self.blocked(ip)
        if blocked:
            return blocked
        with self.lock:
            if ip in self.inflight or len(self.inflight) >= 2:
                return "Подождите пару секунд и попробуйте снова."
            self.inflight.add(ip)
        self.failed(ip)
        return None

    def end_attempt(self, ip: str, ok: bool):
        with self.lock:
            self.inflight.discard(ip)
            if ok:
                if self.global_fails:
                    self.global_fails.pop()  # снимаем занятую заранее попытку
                self.trusted[ip] = self.clock()
                self.ip_locked.pop(ip, None)  # верный пароль пятой попыткой — не блокируем
        if ok:
            self.succeeded(ip)

    def blocked(self, ip: str) -> str | None:
        now = self.clock()
        with self.lock:
            trusted = now - self.trusted.get(ip, -TRUST_DAYS) < TRUST_DAYS
            if now < self.global_locked_until and not trusted:
                # Адрес, с которого уже входили верно, общая блокировка не касается:
                # иначе любой мог бы держать владельца снаружи бесконечно.
                return "Слишком много неверных паролей — вход закрыт на час."
            if now < self.ip_locked.get(ip, 0):
                return "Слишком много неверных паролей с этого адреса — попробуйте через 15 минут."
        return None

    def failed(self, ip: str):
        now = self.clock()
        with self.lock:
            fails = [t for t in self.ip_fails.get(ip, []) if now - t < IP_LOCK] + [now]
            self.ip_fails[ip] = fails
            if len(fails) >= IP_FAILS:
                self.ip_locked[ip] = now + IP_LOCK
                self.ip_fails[ip] = []
                log.warning("Адрес %s заблокирован на 15 минут: %d неверных паролей", ip, IP_FAILS)
            self.global_fails = [t for t in self.global_fails if now - t < GLOBAL_WINDOW] + [now]
            if len(self.global_fails) >= GLOBAL_FAILS:
                self.global_locked_until = now + GLOBAL_WINDOW
                self.global_fails = []
                log.warning("Вход закрыт на час: %d неверных паролей за час", GLOBAL_FAILS)

    def succeeded(self, ip: str):
        with self.lock:
            self.ip_fails.pop(ip, None)

    def new_session(self) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        now = self.clock()
        with self.lock:
            self.sessions[token] = {"csrf": csrf, "created": now, "seen": now}
        return token, csrf

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        now = self.clock()
        with self.lock:
            s = self.sessions.get(token)
            if not s or now - s["seen"] > IDLE or now - s["created"] > ABSOLUTE:
                self.sessions.pop(token, None)
                return None
            s["seen"] = now
            return s

    def end(self, token: str | None):
        with self.lock:
            self.sessions.pop(token or "", None)


# --- страницы ------------------------------------------------------------------

STYLE = """<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1c1f24;--muted:#636b76;--line:#dfe3e8;--accent:#2f6fde;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--card:#1e2125;--text:#e8eaed;--muted:#9aa2ad;--line:#30353b;--accent:#6e9cf0;--err:#f08a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:420px;margin:0 auto;padding:48px 16px}h1{font-size:22px;margin:0 0 16px}
form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
label{display:block;font-weight:600;margin-bottom:4px;font-size:14px}
input{width:100%;padding:10px 12px;font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:8px}
button{margin-top:14px;font:inherit;font-weight:600;border:0;border-radius:8px;padding:11px 16px;background:var(--accent);color:#fff;cursor:pointer;width:100%}
.err{color:var(--err);margin:0 0 12px;font-size:14px}.muted{color:var(--muted);font-size:13px;margin-top:12px}
</style>"""

LOGIN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
<title>Вход — финансовый помощник</title>""" + STYLE + """</head><body><main>
<h1>Настройки финансового помощника</h1>
{message}
<form method="post" action="/login" autocomplete="off">
<label for="pw">Пароль</label>
<input id="pw" name="password" type="password" autofocus required autocomplete="current-password">
<button type="submit">Войти</button>
</form>
<p class="muted">Пароль задаётся на сервере: sudo bash /opt/finance-bot/deploy/settings.sh --password</p>
</main></body></html>"""

def login_page(message: str = "") -> str:
    # Не .format(): в CSS полно фигурных скобок.
    return LOGIN_PAGE.replace("{message}", message)


TOPBAR = """<form method="post" action="/logout" style="float:right;margin:0">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit" class="secondary" style="margin:0">Выйти</button></form>"""

SUBTITLE = ("Изменения вступают в силу сами: бот перезапустится примерно через 20 секунд. "
            "Пустое поле — оставить текущее значение.")


class SettingsServer(ThreadingHTTPServer):
    daemon_threads = True
    # На Windows SO_REUSEADDR позволяет занять уже занятый порт — тогда страница
    # «запустилась бы» поверх чужой программы. Там порт должен быть свободен.
    allow_reuse_address = os.name != "nt"

    def __init__(self, addr, env_path: str, password_hash: str, *, checks=True, guard=None,
                 secure_cookie=True):
        super().__init__(addr, SettingsHandler)
        self.env_path = env_path
        self.password_hash = password_hash
        self.checks = checks
        self.guard = guard or Guard()
        self.secure_cookie = secure_cookie

    def handle_error(self, request, client_address):
        # Порт открыт в интернет: сканеры и незашифрованные подключения — обычное
        # дело. Коротко в журнал, без трассировок.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLEOFError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            log.debug("Соединение %s оборвано: %s", client_address[0], type(exc).__name__)
            return
        if isinstance(exc, ssl.SSLError) and getattr(exc, "reason", "") == "HTTP_REQUEST":
            log.info("Отклонено незашифрованное подключение (%s)", client_address[0])
            return
        if isinstance(exc, (ssl.SSLError, ConnectionError, OSError)):
            # Браузер закрыл соединение при рукопожатии (например, до принятия
            # самоподписанного сертификата) — не событие.
            log.debug("Соединение %s: %s", client_address[0], exc)
            return
        log.exception("Ошибка страницы настроек (%s)", client_address[0])


class SettingsHandler(BaseHTTPRequestHandler):
    server: SettingsServer
    timeout = 30  # зависшее соединение не держит поток вечно

    def log_message(self, fmt, *args):
        pass

    @property
    def ip(self) -> str:
        return self.client_address[0]

    def _send(self, code: int, body: str = "", ctype="text/html; charset=utf-8",
              headers: dict | None = None, nonce: str = ""):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        script = f"'nonce-{nonce}'" if nonce else "'none'"
        self.send_header("Content-Security-Policy",
                         f"default-src 'none'; style-src 'unsafe-inline'; script-src {script}; "
                         "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str, cookie: str | None = None):
        headers = {"Location": to}
        if cookie is not None:
            headers["Set-Cookie"] = cookie
        self._send(303, "", "text/plain", headers)

    def _cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self.server.secure_cookie else ""
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def _token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: BLE001 — кривой cookie = нет сессии
            return None
        return jar[COOKIE].value if COOKIE in jar else None

    def _form(self) -> dict[str, str] | None:
        return setup_web.read_form(self)

    # --- маршруты ---

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            self._send(200, login_page(""))
            return
        if path != "/":
            self._send(404, "Not found", "text/plain")
            return
        session = self.server.guard.session(self._token())
        if not session:
            self._redirect("/login")
            return
        self._settings_page(session)

    def do_POST(self):
        path = urlparse(self.path).path
        form = self._form()
        if form is None:
            return
        if path == "/login":
            self._login(form)
            return
        token = self._token()
        session = self.server.guard.session(token)
        if not session:
            self._redirect("/login")
            return
        if not hmac.compare_digest(form.get("csrf", ""), session["csrf"]):
            self._send(403, "Forbidden", "text/plain")
            return
        if path == "/logout":
            self.server.guard.end(token)
            log.info("Выход (%s)", self.ip)
            self._redirect("/login", self._cookie("", 0))
        elif path == "/whois":
            env_token = form.get("TELEGRAM_BOT_TOKEN") or read_env(self.server.env_path).get(
                "TELEGRAM_BOT_TOKEN", "")
            try:
                if not env_token:
                    raise ValueError("сначала впишите токен бота")
                result = {"users": setup_web.recent_senders(env_token)}
            except (ValueError, OSError) as exc:
                result = {"error": str(exc)}
            self._send(200, json.dumps(result, ensure_ascii=False), "application/json")
        elif path == "/":
            self._save(session, form)
        else:
            self._send(404, "Not found", "text/plain")

    def _login(self, form: dict):
        guard = self.server.guard
        blocked = guard.begin_attempt(self.ip)
        if blocked:
            self._send(429, login_page(f"<p class='err'>{html.escape(blocked)}</p>"))
            return
        ok = False
        try:
            ok = verify_password(form.get("password", ""), self.server.password_hash)
        finally:
            guard.end_attempt(self.ip, ok)
        if ok:
            token, _ = guard.new_session()
            log.info("Вход в настройки (%s)", self.ip)
            self._redirect("/", self._cookie(token, ABSOLUTE))
            return
        log.warning("Неверный пароль (%s)", self.ip)
        time.sleep(1)  # замедляем подбор
        self._send(401, login_page("<p class='err'>Неверный пароль.</p>"))

    def _settings_page(self, session: dict, message: str = "", form: dict | None = None):
        env = read_env(self.server.env_path)
        ids = env.get("ALLOWED_USER_IDS", "").replace(",", " ").split()
        form = form or {}
        now = lambda key, what: (f"Сейчас: {html.escape(mask(env[key]))}" if env.get(key)
                                 else f"Сейчас: {what} не задан")
        nonce = secrets.token_urlsafe(12)
        page = PAGE.format(
            topbar=TOPBAR.format(csrf=session["csrf"]), subtitle=SUBTITLE,
            script_nonce=f' nonce="{nonce}"', message=message, action="", csrf=session["csrf"],
            key_now=now("ANTHROPIC_API_KEY", "ключ"), token_now=now("TELEGRAM_BOT_TOKEN", "токен"),
            test_now=setup_web.test_now(env),
            user1=html.escape(form.get("user1", ids[0] if ids else "")),
            user2=html.escape(form.get("user2", ids[1] if len(ids) > 1 else "")))
        # Форма шлёт на «/», проверка «кто писал» — на «/whois» (action="" → "/whois").
        page = page.replace('action=""', 'action="/"', 1)
        self._send(200, page, nonce=nonce)

    def _save(self, session: dict, form: dict):
        env = read_env(self.server.env_path)
        updates, errors, notes = {}, [], []
        key = form.get("ANTHROPIC_API_KEY", "")
        token = form.get("TELEGRAM_BOT_TOKEN", "")
        errors += setup_web.format_errors(form)
        if errors:
            key = token = ""
            form = {k: v for k, v in form.items() if not k.startswith(("TELEGRAM", "ANTHROPIC"))}
        try:
            ids = parse_ids(" ".join(x for x in (form.get("user1", ""), form.get("user2", "")) if x))
            updates["ALLOWED_USER_IDS"] = ",".join(map(str, ids))
        except ValueError as exc:
            errors.append(str(exc))
        if self.server.checks:
            for value, check, label in ((key, setup_web.check_anthropic, "Claude API"),
                                        (token, setup_web.check_telegram, "Бот")):
                if value:
                    try:
                        notes.append(f"{label}: {check(value)}")
                    except ValueError as exc:
                        errors.append(str(exc))
        setup_web.test_bot_updates(form, env, token, self.server.checks, updates, errors, notes)
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
            self._settings_page(session, msg, form)
            return
        write_env(self.server.env_path, updates)
        log.info("Настройки изменены (%s): %s", self.ip, ", ".join(sorted(updates)))
        notes.append("Сохранено. Боты перезапустятся сами примерно через 20 секунд.")
        msg = "<div class='msg ok'>" + "<br>".join(html.escape(n) for n in notes) + "</div>"
        self._settings_page(session, msg)


def _read_password_hash(folder: str) -> str:
    path = os.path.join(folder, PASSWORD_FILE)
    if not os.path.exists(path):
        sys.exit("Пароль не задан. Задайте его: sudo bash /opt/finance-bot/deploy/settings.sh --password")
    with open(path) as fh:
        return fh.read().strip()


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .logfilter import install_secret_filter
    install_secret_filter()
    p = argparse.ArgumentParser(description="Постоянная страница настроек под паролем")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("set-password")
    sp.add_argument("--dir", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--env", required=True)
    sv.add_argument("--dir", required=True, help="папка с паролем и сертификатом")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--bind", default="0.0.0.0")
    args = p.parse_args(argv)

    if args.cmd == "set-password":
        while True:
            first = getpass.getpass("Новый пароль для страницы настроек: ")
            problem = check_new_password(first)
            if problem:
                print(f"  {problem}, попробуйте ещё раз")
                continue
            if getpass.getpass("Повторите пароль: ") != first:
                print("  пароли не совпали, ещё раз")
                continue
            break
        save_password_file(args.dir, first)
        print("Пароль сохранён.")
        return

    server = SettingsServer((args.bind, args.port), args.env, _read_password_hash(args.dir))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(os.path.join(args.dir, "cert.pem"), os.path.join(args.dir, "key.pem"))
    # Рукопожатие TLS — в потоке соединения, а не в общем цикле приёма:
    # медленный клиент не задерживает остальных.
    server.socket = ctx.wrap_socket(server.socket, server_side=True,
                                    do_handshake_on_connect=False)
    log.info("Страница настроек: https://<адрес сервера>:%d", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
