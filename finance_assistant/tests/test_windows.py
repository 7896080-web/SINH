"""Версия для Windows: супервизор, бэкап, подготовка .env, сертификат, часовой пояс.

Сама Windows здесь недоступна — проверяется логика, которую зовут скрипты
deploy/windows/*.ps1.
"""
import datetime
import os
import sqlite3
import sys
import time
import zipfile

import pytest

from finance import backup, clock, supervise, windows
from finance.setup_web import read_env, write_env
from finance.storage import Storage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- часовой пояс -------------------------------------------------------------

def test_local_today_uses_finance_tz(monkeypatch):
    fixed = datetime.datetime(2026, 9, 26, 22, 30, tzinfo=datetime.timezone.utc)

    class FakeDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(clock, "datetime", FakeDatetime)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setenv("FINANCE_TZ", "Europe/Moscow")        # 22:30 UTC = 01:30 МСК
    assert clock.local_today() == datetime.date(2026, 9, 27)
    monkeypatch.setenv("FINANCE_TZ", "America/New_York")
    assert clock.local_today() == datetime.date(2026, 9, 26)
    monkeypatch.setenv("FINANCE_TZ", "Нет/Такого")            # неизвестный — время сервера
    assert clock.local_today() == datetime.date.today()


# --- супервизор ---------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=1:A\nTZ=Europe/Moscow\n", encoding="utf-8")
    return tmp_path


def test_wanted_processes_and_settings_page_toggle(app):
    sup = supervise.Supervisor(str(app), python="py")
    assert sup.wanted() == {"bot": ["py", "-m", "finance"],
                            "bot-test": ["py", "-m", "finance", "--test"]}
    settings = app / "settings"
    settings.mkdir()
    (settings / "enabled").write_text("8765")
    assert "settings" not in sup.wanted()                  # пароля и сертификата ещё нет
    for name in ("password", "cert.pem", "key.pem"):
        (settings / name).write_text("x")
    assert sup.wanted()["settings"][-2:] == ["--port", "8765"]
    (settings / "enabled").write_bytes(b"\xef\xbb\xbf9443\r\n")   # BOM и перевод строки Windows
    assert sup.wanted()["settings"][-1] == "9443"
    (settings / "enabled").write_text("мусор")
    assert "settings" not in sup.wanted()


def test_child_env_reads_dotenv(app, monkeypatch):
    sup = supervise.Supervisor(str(app))
    env = sup.child_env()
    assert env["TELEGRAM_BOT_TOKEN"] == "1:A"
    assert env["FINANCE_ENV_FILE"] == str(app / ".env")
    assert env["FINANCE_DATA_DIR"] == str(app / "data")
    assert env["FINANCE_TEST_DATA_DIR"] == str(app / "data-test")
    assert env["PYTHONUTF8"] == "1"
    monkeypatch.setattr(supervise.os, "name", "nt")
    env = sup.child_env()
    assert "TZ" not in env and env["FINANCE_TZ"] == "Europe/Moscow"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _script(tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def _wait(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_supervisor_restarts_exited_child_and_logs(app, monkeypatch):
    clk = Clock()
    sup = supervise.Supervisor(str(app), clock=clk)
    counter = app / "runs.txt"
    quick = _script(app, "quick", f"open(r'{counter}', 'a').write('x'); print('привет из бота')")
    slow = _script(app, "slow", "import time; time.sleep(30)")
    monkeypatch.setattr(sup, "wanted", lambda: {"bot": quick, "bot-test": slow})
    try:
        sup.tick()
        assert _wait(lambda: sup.children["bot"].proc.poll() is not None)
        sup.tick()                                        # завершился — ждёт RESTART_DELAY
        assert sup.children["bot"].proc is None and counter.read_text() == "x"
        clk.now += supervise.RESTART_DELAY - 1
        sup.tick()
        assert counter.read_text() == "x"                 # упал сразу — пауза растёт
        clk.now += supervise.RESTART_DELAY + 1
        sup.tick()
        assert _wait(lambda: counter.exists() and counter.read_text() == "xx")
        test_proc = sup.children["bot-test"].proc
        assert test_proc.poll() is None                   # долгий процесс не трогаем
        sup.tick()
        assert sup.children["bot-test"].proc is test_proc
        # Выключили процесс (как страницу настроек) — останавливается.
        monkeypatch.setattr(sup, "wanted", lambda: {"bot": quick})
        sup.tick()
        assert "bot-test" not in sup.children and test_proc.poll() is not None
    finally:
        sup.stop_all()
    log = (app / "logs" / "bot.log").read_text(encoding="utf-8")
    assert log.count("запуск: bot") == 2 and "привет из бота" in log


def test_supervisor_passes_dotenv_to_child(app, monkeypatch):
    sup = supervise.Supervisor(str(app), clock=Clock())
    out = app / "env.txt"
    probe = _script(app, "probe", f"import os; open(r'{out}', 'w').write(os.environ['TELEGRAM_BOT_TOKEN'])")
    monkeypatch.setattr(sup, "wanted", lambda: {"bot": probe})
    try:
        sup.tick()
        assert _wait(out.exists) and _wait(lambda: out.read_text() == "1:A")
    finally:
        sup.stop_all()


def test_log_rotation(app, monkeypatch):
    monkeypatch.setattr(supervise, "LOG_LIMIT", 10)
    sup = supervise.Supervisor(str(app))
    (app / "logs").mkdir()
    (app / "logs" / "bot.log").write_text("x" * 100)
    sup._open_log("bot").close()
    assert (app / "logs" / "bot.log.1").stat().st_size == 100
    assert (app / "logs" / "bot.log").stat().st_size == 0


def test_is_ours():
    assert supervise.is_ours(["python.exe", "-m", "finance"])
    assert supervise.is_ours(["C:\\venv\\python.exe", "-m", "finance", "--test"])
    assert supervise.is_ours(["python", "-m", "finance.settings_web", "serve"])
    assert not supervise.is_ours(["python", "-m", "finance.supervise", "--app", "x"])
    assert not supervise.is_ours(["python", "-m", "http.server"])
    assert not supervise.is_ours(["notepad.exe", "finance"])
    assert not supervise.is_ours([])


# --- бэкап --------------------------------------------------------------------

def test_backup_copies_every_user_and_prunes_old(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "backups"
    for uid in ("111", "222"):
        folder = data / "users" / uid
        folder.mkdir(parents=True)
        db = Storage(str(folder / "finance.db"))
        db.add_card("Сбер", uid[-1] * 4, "Сбер")
        db.close()
    (data / "users" / "111" / "receipts" / "2026-09").mkdir(parents=True)
    (data / "users" / "111" / "receipts" / "2026-09" / "a.png").write_bytes(b"png")
    dest.mkdir()
    old = dest / "finance-111-20200101-0000.db"
    old.write_text("старый")
    os.utime(old, (0, 0))
    old_zip = dest / "receipts-111-20200101-0000.zip"           # архив прежней версии
    old_zip.write_bytes(b"zip")
    os.utime(old_zip, (0, 0))
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=1:A\n", encoding="utf-8")
    assert backup.backup(str(data), str(dest), keep_days=30, stamp="20260926-0330",
                         env_file=str(env)) == 2
    names = sorted(os.listdir(dest))
    assert names == ["env-latest.txt", "finance-111-20260926-0330.db",
                     "finance-222-20260926-0330.db", "receipts-111"]
    copy = sqlite3.connect(dest / "finance-222-20260926-0330.db")
    assert copy.execute("SELECT name FROM cards").fetchone()[0] == "Сбер"
    copy.close()
    mirrored = dest / "receipts-111" / "2026-09" / "a.png"
    assert mirrored.read_bytes() == b"png"
    # Следующий день: скриншоты не копируются заново, только новые.
    (data / "users" / "111" / "receipts" / "2026-09" / "b.png").write_bytes(b"png2")
    os.utime(mirrored, (0, 0))
    backup.backup(str(data), str(dest), keep_days=30, stamp="20260927-0330")
    assert (dest / "receipts-111" / "2026-09" / "b.png").exists()
    assert os.path.getmtime(mirrored) == 0                       # старый не трогали


def test_backup_frees_space_first(tmp_path, monkeypatch):
    """Диск полон: копия падает, но старые копии к этому моменту уже удалены."""
    data, dest = tmp_path / "data", tmp_path / "backups"
    (data / "users" / "1").mkdir(parents=True)
    Storage(str(data / "users" / "1" / "finance.db")).close()
    dest.mkdir()
    old = dest / "finance-1-20200101-0000.db"
    old.write_text("x")
    os.utime(old, (0, 0))

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(backup, "_copy_db", full)
    with pytest.raises(OSError):
        backup.backup(str(data), str(dest))
    assert not old.exists()


# --- .env и сертификат -------------------------------------------------------

def test_init_env_sets_windows_paths_and_keeps_tokens(tmp_path):
    app = tmp_path / "FinanceBot"
    app.mkdir()
    template = os.path.join(ROOT, ".env.example")
    env_path = windows.init_env(str(app), template)
    env = read_env(env_path)
    assert env["FINANCE_DATA_DIR"] == str(app / "data")
    assert env["FINANCE_TEST_DATA_DIR"] == str(app / "data-test")
    assert windows.missing(str(app)) == ["TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"]
    write_env(env_path, {"TELEGRAM_BOT_TOKEN": "1:A", "ANTHROPIC_API_KEY": "k",
                         "FINANCE_DATA_DIR": "D:\\Мои данные"})
    windows.init_env(str(app), template)                  # повторно (обновление)
    env = read_env(env_path)
    assert env["TELEGRAM_BOT_TOKEN"] == "1:A" and env["FINANCE_DATA_DIR"] == "D:\\Мои данные"
    assert windows.missing(str(app)) == []


def test_cert_created_once_and_renewed_before_expiry(tmp_path):
    ssl = pytest.importorskip("ssl")
    pytest.importorskip("cryptography")
    folder = str(tmp_path / "settings")
    now = datetime.datetime(2026, 9, 27, tzinfo=datetime.timezone.utc)
    assert windows.make_cert(folder, now) is True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem"))
    assert windows.make_cert(folder, now + datetime.timedelta(days=600)) is False
    assert windows.make_cert(folder, now + datetime.timedelta(days=710)) is True


def test_windows_scripts_are_utf8_with_bom():
    """PowerShell 5 читает скрипт без BOM в кодировке ANSI — русский текст
    превратился бы в кашу, а скрипт мог бы не разобраться."""
    folder = os.path.join(ROOT, "deploy", "windows")
    scripts = [n for n in os.listdir(folder) if n.endswith(".ps1")]
    assert {"install.ps1", "settings.ps1", "status.ps1"} <= set(scripts)
    for name in scripts:
        raw = open(os.path.join(folder, name), "rb").read()
        assert raw.startswith(b"\xef\xbb\xbf"), name
        raw.decode("utf-8")


def test_read_env_tolerates_notepad(tmp_path):
    """Блокнот сохраняет с BOM или в кодировке Windows — настройки читаются."""
    p = tmp_path / ".env"
    p.write_bytes("﻿TELEGRAM_BOT_TOKEN=1:A\r\n# коммент\r\nTZ=Europe/Moscow\r\n".encode("utf-8"))
    assert read_env(str(p)) == {"TELEGRAM_BOT_TOKEN": "1:A", "TZ": "Europe/Moscow"}
    p.write_bytes("# русский комментарий\r\nTELEGRAM_BOT_TOKEN=1:A\r\n".encode("cp1251"))
    assert read_env(str(p)) == {"TELEGRAM_BOT_TOKEN": "1:A"}
    write_env(str(p), {"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert read_env(str(p))["ANTHROPIC_API_KEY"] == "sk-ant-x"


def test_supervisor_survives_errors_and_filters_env(app, monkeypatch):
    (app / ".env").write_text("TELEGRAM_BOT_TOKEN=1:A\nLD_PRELOAD=/tmp/evil.so\nPYTHONPATH=/x\n",
                              encoding="utf-8")
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    sup = supervise.Supervisor(str(app))
    env = sup.child_env("bot")
    assert env["TELEGRAM_BOT_TOKEN"] == "1:A" and "LD_PRELOAD" not in env
    assert "PYTHONPATH" not in env
    assert "TELEGRAM_BOT_TOKEN" not in sup.child_env("settings")   # странице токены не нужны
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сбой")
        sup.stopping = True
    monkeypatch.setattr(sup, "tick", boom)
    monkeypatch.setattr(supervise, "RESTART_DELAY", 0)
    monkeypatch.setattr(supervise, "TICK", 0)
    monkeypatch.setattr(supervise, "kill_leftovers", lambda app: None)
    sup.run()                                                # не упал на первом сбое
    assert len(calls) == 2
