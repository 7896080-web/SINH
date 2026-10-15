"""Накат берёт ТОЛЬКО локальную копию — иначе он стоит на ней десятками минут.

15.10 на pm160 накат встал на шаге «Копия базы перед миграцией...» и молчал.
Причина не в бэкапе: тот же скрипт после локальной копии везёт её на вторую
площадку, а выгрузка на медленном канале идёт десятками минут (замер: WebDAV
Яндекса, 111 КиБ/с, 305 МБ — сорок шесть минут) при пределе в четыре часа.

Цена не в ожидании. Накат стоит в САМОМ НЕПРИЯТНОМ из возможных состояний:
новые файлы на диске уже лежат, службы работают ещё на старом коде — ровно то,
что накат и должен проходить насквозь, не останавливаясь.

Разделение по вопросу, на который копия отвечает: копия наката — «чем откатить
миграцию через десять минут», значит нужна на диске и СРАЗУ; вторая площадка —
«что если откажет диск», и у неё своё суточное расписание.
"""
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import backup

ROOT = Path(__file__).resolve().parent.parent


class Rclone:
    """Подставной rclone, который помнит, звали ли его вообще."""

    def __init__(self, listing=None):
        self.subcommands = []
        self.listing = listing if listing is not None else []

    def __call__(self, command, capture_output=True, timeout=None, **kw):
        sub = command[command.index("--log-level") + 2]
        self.subcommands.append(sub)
        if sub == "lsjson":
            return subprocess.CompletedProcess(command, 0,
                                               json.dumps(self.listing), "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    """Настоящая база на диске, каталог копий, папка-зеркало и облако."""
    source = tmp_path / "sync_admin.db"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE products (uid_1c TEXT)")
    con.execute("INSERT INTO products VALUES ('u1')")
    con.commit()
    con.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{source}")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.setenv("BACKUP_RCLONE_REMOTE", "hbox:sync_admin_backups")
    monkeypatch.setenv("BACKUP_RCLONE_EXE", str(tmp_path / "rclone.exe"))
    monkeypatch.setenv("BACKUP_RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    return tmp_path


def _script():
    spec = importlib.util.spec_from_file_location(
        "backup_db_script", ROOT / "scripts" / "backup_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------- сама копия без облака

def test_a_local_only_copy_never_touches_the_cloud(live_db, monkeypatch):
    """Главная проверка: копия снята и проверена, а в облако не ходили вовсе."""
    fake = Rclone()
    monkeypatch.setattr(subprocess, "run", fake)

    result = backup.make_backup(offsite=False)

    assert result.ok and result.checked, "копия не снята или не проверена"
    assert Path(result.path).is_file()
    assert fake.subcommands == [], (
        "накат пошёл в облако — значит он снова будет стоять на этом шаге "
        "десятками минут, пока на диске новый код, а службы на старом")


def test_a_local_only_copy_skips_the_mirror_folder_too(live_db, monkeypatch):
    """Папка-зеркало такая же вторая площадка: сетевая шара отвечает минутами,
    а то и не отвечает вовсе."""
    monkeypatch.setattr(subprocess, "run", Rclone())

    result = backup.make_backup(offsite=False)

    assert not (live_db / "mirror").exists(), "накат трогал вторую площадку"
    assert not result.mirror_error, (
        "оговорка про зеркало при `offsite=False` — неправда: мы туда и не "
        "собирались")


def test_by_default_the_copy_still_goes_offsite(live_db, monkeypatch):
    """Обратная сторона: суточное задание обязано везти копию дальше.

    Без этой проверки правка однажды выключила бы вторую площадку ВСЕМ, и
    узналось бы это в день отказа диска."""
    fake = Rclone()
    monkeypatch.setattr(subprocess, "run", fake)

    result = backup.make_backup()

    assert "copyto" in fake.subcommands, "копия не поехала в облако"
    assert (live_db / "mirror").exists(), "копия не легла в папку-зеркало"
    assert result.ok


# ------------------------------------------------------------- ключ у скрипта

def test_the_script_flag_skips_the_second_site(live_db, monkeypatch, capsys):
    fake = Rclone()
    monkeypatch.setattr(subprocess, "run", fake)

    assert _script().main(["--local-only"]) == 0
    assert fake.subcommands == []


def test_calling_main_without_arguments_is_not_the_process_command_line(
        live_db, monkeypatch):
    """`main()` без аргументов — это «без аргументов», а не `sys.argv`.

    Умолчание argparse читает командную строку ПРОЦЕССА, а этот `main` зовут и
    в тестах, и из приложения: под pytest там лежит «tests/... -q», и скрипт
    падал на «unrecognized arguments». Поймано тремя соседними тестами,
    которые зовут его именно так."""
    monkeypatch.setattr(subprocess, "run", Rclone())

    assert _script().main() == 0


def test_the_script_says_the_second_site_was_not_touched(live_db, monkeypatch,
                                                         capsys):
    """«Копия снята» без оговорки читается как «и в облаке тоже»."""
    monkeypatch.setattr(subprocess, "run", Rclone())

    _script().main(["--local-only"])

    out = capsys.readouterr().out
    assert "вторая площадка не тронута" in out
    assert "суточное задание" in out, "не сказано, чья это теперь работа"


def test_the_script_warns_before_the_slow_step(live_db, monkeypatch, capsys):
    """Про «медленно» надо сказать ДО него, а не после.

    Человек запускает скрипт руками и смотрит в консоль: сорок шесть минут
    тишины он читает как «повисло» — ровно так и прочитали накат 15.10.

    Спрашиваем не порядок строк в итоговом выводе, а то, что было НА ЭКРАНЕ В
    МОМЕНТ начала долгого шага: по порядку строк проверка проходила и тогда,
    когда предупреждение печаталось уже после копии — то есть была зелёной по
    неверной причине (поймано мутацией)."""
    module = _script()
    real = module.make_backup
    on_screen = {}

    def spy(*args, **kwargs):
        on_screen["text"] = capsys.readouterr().out
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "make_backup", spy)
    monkeypatch.setattr(subprocess, "run", Rclone())

    module.main([])

    assert "не зависание" in on_screen.get("text", ""), (
        "в момент начала долгой выгрузки на экране ещё ничего про неё не было — "
        "то есть человек видит тишину и решает, что скрипт повис")


def test_the_warning_is_silent_without_a_cloud(live_db, monkeypatch, capsys):
    """Облака нет — предупреждать не о чем, а лишняя строка приучает не читать."""
    monkeypatch.delenv("BACKUP_RCLONE_REMOTE")
    monkeypatch.setattr(subprocess, "run", Rclone())

    _script().main([])

    assert "не зависание" not in capsys.readouterr().out


# --------------------------------------------------------- и накат его передаёт

def test_the_deploy_script_passes_the_flag():
    """Ключ обязан стоять в САМОМ накате, иначе правка ничего не меняет.

    PowerShell здесь не запустить, поэтому проверяется строка вызова: это
    сильнейшая из доступных на машине разработки проверок, и она ровно про то
    место, где дефект и был. Тот же приём, которым проверяются адреса в
    `create_desktop_shortcut.ps1`.
    """
    script = (ROOT / "deploy" / "update_windows.ps1").read_text(encoding="utf-8-sig")
    calls = [line for line in script.splitlines() if "backup_db.py" in line
             and line.lstrip().startswith("&")]
    assert calls, "накат вообще не снимает копию перед миграцией"
    for line in calls:
        assert "--local-only" in line, (
            f"накат снимает копию со второй площадкой: {line.strip()} — он "
            "будет стоять на этом шаге десятками минут")
