"""Супервизор для Windows: держит запущенными боевой бот, тестовый бот и
страницу настроек (на Linux то же делает systemd).

    python -m finance.supervise --app C:\\FinanceBot

Запускается Планировщиком заданий при включении сервера. Что делает:
- читает настройки из <app>\\.env и передаёт их процессам (на Linux это
  делает EnvironmentFile в службе);
- упавший или завершившийся процесс запускает снова через RESTART_DELAY —
  бот сам завершается, когда на странице настроек поменяли .env, и так
  подхватывает новые настройки;
- страницу настроек запускает, только пока она включена (файл
  <app>\\settings\\enabled с номером порта — его ставит settings.ps1) и задан
  пароль; выключили — останавливает;
- вывод каждого процесса пишет в <app>\\logs\\<имя>.log.

При старте останавливает процессы бота, оставшиеся от прошлого запуска
супервизора: два экземпляра одного бота отнимали бы друг у друга сообщения.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

from .logfilter import install_secret_filter
from .setup_web import read_env

log = logging.getLogger("finance.supervise")

RESTART_DELAY = 10      # сек между перезапусками упавшего процесса
MAX_DELAY = 300         # потолок паузы, если процесс падает сразу после запуска раз за разом
QUICK_EXIT = 60         # прожил меньше — «упал сразу» (нет сети при загрузке, неверный токен)
TICK = 1.0              # сек между проверками
LOG_LIMIT = 5 * 1024 * 1024  # больше — старый журнал уходит в .1 при перезапуске
MODULES = ("finance", "finance.settings_web")
# Что из .env передаётся процессам. Остальное (например, чужой LD_PRELOAD или
# PYTHONPATH, вписанный в .env) не передаётся.
ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_TEST", "ANTHROPIC_API_KEY",
            "ALLOWED_USER_IDS", "TEST_USER_IDS", "FINANCE_DATA_DIR", "FINANCE_TEST_DATA_DIR",
            "TZ", "FINANCE_TZ", "CLAUDE_MODEL",
            # Прокси для выхода к api.anthropic.com / api.telegram.org, если нужен.
            "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
            "https_proxy", "http_proxy", "all_proxy", "no_proxy")


@dataclass
class Child:
    name: str
    args: list[str]
    proc: subprocess.Popen | None = None
    next_start: float = 0.0
    started: float = 0.0
    quick_fails: int = 0
    log_file: object = field(default=None, repr=False)


class Supervisor:
    def __init__(self, app: str, python: str = sys.executable, clock=time.monotonic):
        self.app = os.path.abspath(app)
        self.python = python
        self.clock = clock
        self.env_path = os.path.join(self.app, ".env")
        self.settings_dir = os.path.join(self.app, "settings")
        self.logs = os.path.join(self.app, "logs")
        self.children: dict[str, Child] = {}
        self.stopping = False

    # --- что должно работать ------------------------------------------------

    def settings_port(self) -> int | None:
        """Порт страницы настроек, если она включена и пароль задан."""
        try:
            with open(os.path.join(self.settings_dir, "enabled"), encoding="utf-8-sig") as fh:
                port = int(fh.read().strip() or 0)
        except (OSError, ValueError):
            return None
        if not (0 < port < 65536):
            return None
        needed = ("password", "cert.pem", "key.pem")
        if not all(os.path.exists(os.path.join(self.settings_dir, n)) for n in needed):
            return None
        return port

    def wanted(self) -> dict[str, list[str]]:
        py = self.python
        want = {"bot": [py, "-m", "finance"],
                "bot-test": [py, "-m", "finance", "--test"]}
        port = self.settings_port()
        if port:
            want["settings"] = [py, "-m", "finance.settings_web", "serve", "--env", self.env_path,
                                "--dir", self.settings_dir, "--port", str(port)]
        return want

    def child_env(self, name: str = "bot") -> dict[str, str]:
        env = dict(os.environ)
        if name != "settings":  # странице настроек токены не нужны: она сама читает .env
            dotenv = read_env(self.env_path)
            env.update({k: dotenv[k] for k in ENV_KEYS if k in dotenv})
        env["FINANCE_ENV_FILE"] = self.env_path
        env.setdefault("FINANCE_DATA_DIR", os.path.join(self.app, "data"))
        env.setdefault("FINANCE_TEST_DATA_DIR", os.path.join(self.app, "data-test"))
        env["PYTHONUTF8"] = "1"              # русские буквы в журналах и путях
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"        # журнал пишется сразу, а не пачками
        if os.name == "nt" and "TZ" in env:
            # Windows понимает TZ только в виде «MSK-3» и путает время при
            # «Europe/Moscow»; бот считает дату сам по FINANCE_TZ.
            env.setdefault("FINANCE_TZ", env["TZ"])
            del env["TZ"]
        return env

    # --- процессы -----------------------------------------------------------

    def _open_log(self, name: str):
        os.makedirs(self.logs, exist_ok=True)
        path = os.path.join(self.logs, f"{name}.log")
        try:
            if os.path.getsize(path) > LOG_LIMIT:
                os.replace(path, path + ".1")
        except OSError:
            pass
        return open(path, "ab", buffering=0)

    def start(self, child: Child):
        child.log_file = self._open_log(child.name)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        child.log_file.write(f"\n===== {stamp} запуск: {child.name} =====\n".encode("utf-8"))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        child.proc = subprocess.Popen(child.args, cwd=self.app, env=self.child_env(child.name),
                                      stdin=subprocess.DEVNULL, stdout=child.log_file,
                                      stderr=subprocess.STDOUT, creationflags=flags)
        log.info("Запущен %s (pid %s)", child.name, child.proc.pid)

    def stop(self, child: Child, timeout: float = 10):
        proc = child.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
            log.info("Остановлен %s", child.name)
        child.proc = None
        if child.log_file:
            child.log_file.close()
            child.log_file = None

    def _log_too_big(self, child: Child) -> bool:
        try:
            return os.path.getsize(os.path.join(self.logs, f"{child.name}.log")) > LOG_LIMIT
        except OSError:
            return False

    def tick(self):
        now = self.clock()
        want = self.wanted()
        for name in list(self.children):
            child = self.children[name]
            if name not in want or want[name] != child.args:
                self.stop(child)  # выключили (или сменили порт страницы настроек)
                del self.children[name]
        for name, args in want.items():
            child = self.children.setdefault(name, Child(name, args))
            if child.proc is not None:
                code = child.proc.poll()
                if code is None:
                    if self._log_too_big(child):
                        # Журнал процесса переоткрывается только при запуске:
                        # перезапуск и есть ротация (бот теряет секунды, не данные).
                        log.info("%s: журнал больше %d МБ — перезапуск для ротации",
                                 name, LOG_LIMIT // 2**20)
                        self.stop(child)
                        child.next_start = now
                    else:
                        continue
                else:
                    # Падает сразу после запуска раз за разом — увеличиваем паузу,
                    # чтобы не забивать журнал (сеть ещё не поднялась, токен отозван).
                    child.quick_fails = (child.quick_fails + 1
                                         if now - child.started < QUICK_EXIT else 0)
                    delay = min(RESTART_DELAY * 2 ** child.quick_fails, MAX_DELAY)
                    log.warning("%s завершился (код %s) — перезапуск через %d с",
                                name, code, delay)
                    self.stop(child)
                    child.next_start = now + delay
            if now >= child.next_start:
                try:
                    child.started = now
                    self.start(child)
                except Exception as exc:  # noqa: BLE001 — супервизор не должен падать
                    log.error("Не удалось запустить %s: %s", name, exc)
                    child.next_start = now + RESTART_DELAY

    def stop_all(self):
        for child in self.children.values():
            self.stop(child)
        self.children.clear()

    def run(self):
        kill_leftovers(self.app)
        try:
            while not self.stopping:
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — упадёт супервизор, упадут и боты
                    log.exception("Ошибка супервизора — продолжаю")
                    time.sleep(RESTART_DELAY)
                time.sleep(TICK)
        finally:
            self.stop_all()


def is_ours(cmdline: list[str]) -> bool:
    """Командная строка процесса бота или страницы настроек (python -m finance…)."""
    for i, arg in enumerate(cmdline[:-1]):
        if arg == "-m" and cmdline[i + 1] in MODULES:
            return True
    return False


def kill_leftovers(app: str):
    """Остановить процессы бота, оставшиеся от прежнего супервизора: только
    запущенные из папки программы и только сами боты и страница (не, например,
    `settings_web set-password`, который администратор запустил сейчас)."""
    try:
        import psutil
    except ImportError:
        log.warning("Нет пакета psutil — не проверяю, остались ли процессы от прошлого запуска")
        return
    me = psutil.Process()
    mine = {me.pid} | {p.pid for p in me.parents()}  # venv на Windows запускает python через посредника
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = proc.info["cmdline"] or []
            if proc.pid in mine or not is_ours(cmd) or "set-password" in cmd:
                continue
            if os.path.normcase(proc.cwd()) != os.path.normcase(app):
                continue
            log.warning("Останавливаю оставшийся процесс %s: %s", proc.pid, " ".join(cmd))
            proc.kill()
            proc.wait(10)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass


def main(argv=None):
    p = argparse.ArgumentParser(description="Держит запущенными бота и страницу настроек (Windows)")
    p.add_argument("--app", required=True, help="папка установки, например C:\\FinanceBot")
    args = p.parse_args(argv)
    os.makedirs(os.path.join(args.app, "logs"), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(os.path.join(args.app, "logs", "supervisor.log"),
                                      maxBytes=LOG_LIMIT, backupCount=1, encoding="utf-8")])
    install_secret_filter()
    sup = Supervisor(args.app)

    def stop(*_):
        sup.stopping = True
    for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, stop)
    log.info("Супервизор запущен: %s", sup.app)
    sup.run()
    log.info("Супервизор остановлен")


if __name__ == "__main__":
    main()
