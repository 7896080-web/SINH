# -*- coding: utf-8 -*-
"""
Запуск скрипта по расписанию (Планировщик заданий Windows → wb_schedule.ps1). Сам ничего в кабинетах не меняет.

Зачем вместо «cmd /c python …»:
  • без окна: задание запускает pythonw.exe — чёрное окно не всплывает каждый час и его нельзя случайно закрыть
    посреди применения;
  • своё ограничение по времени: если скрипт завис, он останавливается здесь (а не обрывом cmd, после которого
    python мог остаться висеть), в журнал пишется, что случилось, и снимается его блокировка wb_bids.running;
  • журналы не растут бесконечно: больше MAX_LOG_MB — текущий переименовывается в *.old.txt (прежний .old удаляется);
  • после скрипта — всегда пересборка дашборда (python wb_dashboard.py --no-open).

Запуск:  pythonw wb_run.py wb_bids.py --apply --timeout 55
         pythonw wb_run.py wb_positions.py --timeout 110
Вывод скрипта дописывается в wb_sched_out.txt.
"""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "wb_sched_out.txt"
LOGS = ["wb_sched_out.txt", "wb_bids_log.txt", "wb_guard_log.txt"]
MAX_LOG_MB = 5
LOCK_FILE = HERE / "wb_bids.running"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW: дочерний python без окна


def python_exe():
    """python.exe рядом с pythonw.exe (у дочернего процесса должен быть вывод, который пишется в журнал)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def rotate():
    for name in LOGS:
        p = HERE / name
        try:
            if p.exists() and p.stat().st_size > MAX_LOG_MB * 1024 * 1024:
                old = p.with_name(p.stem + ".old.txt")
                if old.exists():
                    old.unlink()
                p.rename(old)
        except OSError:
            pass


def note(f, text):
    f.write(f"\n[{dt.datetime.now():%d.%m.%Y %H:%M:%S}] {text}\n")
    f.flush()


def release_lock_of(pid):
    """Скрипт остановлен по времени — его блокировка не снимется сама; снимаем, только если она его."""
    try:
        d = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if d.get("pid") == pid:
            LOCK_FILE.unlink()
            return True
    except (OSError, ValueError, AttributeError):
        pass
    return False


def run(args, timeout_min, f):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    note(f, "▶ " + " ".join(args))
    p = subprocess.Popen([python_exe()] + args, cwd=str(HERE), stdout=f, stderr=subprocess.STDOUT, env=env,
                         creationflags=NO_WINDOW)
    try:
        code = p.wait(timeout=timeout_min * 60)
        note(f, f"■ {args[0]}: завершён, код {code}")
        return code
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        freed = release_lock_of(p.pid)
        note(f, f"⏱ {args[0]}: работал дольше {timeout_min} мин — остановлен" + ("; его блокировка снята" if freed else ""))
        return -1


def main():
    args = sys.argv[1:]
    timeout = 55
    if "--timeout" in args:
        i = args.index("--timeout")
        timeout = float(args[i + 1])
        args = args[:i] + args[i + 2:]
    if not args:
        print(__doc__)
        return
    rotate()
    with open(OUT, "a", encoding="utf-8") as f:
        run(args, timeout, f)
        run(["wb_dashboard.py", "--no-open"], 5, f)


if __name__ == "__main__":
    main()
