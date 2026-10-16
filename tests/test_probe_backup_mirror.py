"""`scripts/probe_backup_mirror.py` — прогоняется подпроцессом, как на сервере.

Предмет проверки тут — ТО, ЧТО УВИДИТ ЧЕЛОВЕК В КОНСОЛИ, а не поведение
функций. Скрипт этот запускают ровно тогда, когда что-то уже не так, и
трассировка вместо ответа стоит дороже всего — ровно как у
`restore_offsets_from_backup.py`.

Отдельно про окружение: переменные гасятся ПУСТОЙ СТРОКОЙ, а не удаляются. На
боевом сервере есть `.env`, и `load_dotenv(override=False)` подставил бы оттуда
настоящие пути в удалённые переменные — тест стал бы зелёным здесь и красным
там, то есть остановил бы накат. Пустая строка в окружении для `load_dotenv`
уже «задана», а для настроек — «не задано».
"""
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, WorkerHeartbeat
from app.timeutils import now_utc

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_backup_mirror.py"


@pytest.fixture()
def base(tmp_path):
    """Своя база с одной отметкой бэкапа — и путь к ней."""
    url = f"sqlite:///{tmp_path / 'probe.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add(WorkerHeartbeat(
        worker_name="backup", last_run_at=now_utc(), last_success=True,
        last_error="копия не доехала до зеркала: облако: Ресурс недоступен"))
    session.add(WorkerHeartbeat(worker_name="backup_mirror_ok",
                                last_success=True,
                                last_run_at=now_utc() - timedelta(days=2)))
    session.commit()
    session.close()
    engine.dispose()
    return url


def _run(url, tmp_path, **settings):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    env["BACKUP_DIR"] = str(tmp_path / "backups")
    # Гасим пустой строкой — см. докстроку модуля.
    env["BACKUP_MIRROR_DIR"] = ""
    env["BACKUP_RCLONE_REMOTE"] = ""
    # Кодировка задаётся с ОБЕИХ сторон и одинаковая: у перенаправленного
    # вывода питон берёт её из локали дочернего процесса — здесь UTF-8, на
    # боевом Windows cp1251, — и читай мы его чем-то своим, тест падал бы на
    # мозаике из наших же русских строк, причём только на сервере.
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(settings)
    done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                          encoding="utf-8", errors="replace", env=env,
                          cwd=str(ROOT))
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


def test_it_runs_at_all(base, tmp_path):
    """Самое главное: отвечает, а не падает.

    Сюда приходят с вопросом «почему копия не доехала», и трассировка на этом
    месте означает, что разбираться будут вовсе без скрипта.
    """
    out = _run(base, tmp_path)

    assert "ВТОРАЯ ПЛОЩАДКА КОПИЙ" in out
    assert "Traceback" not in out


def test_an_unconfigured_second_site_says_so_plainly(base, tmp_path):
    """Ненастроенная площадка — не поломка, но и не повод молчать.

    Молчание читалось бы как «вторая площадка в порядке», тогда как все копии
    лежат на том же диске, что и база.
    """
    out = _run(base, tmp_path)

    assert "ВТОРОЙ ПЛОЩАДКИ НЕТ ВОВСЕ" in out
    assert "том же диске" in out


def test_the_folder_mirror_is_counted(base, tmp_path):
    """Копии на второй площадке считаются, а их возраст называется.

    Одна строка «последняя копия 2 дн назад» и отвечает на вопрос, который
    оговорка на «Диагностике» оставляет открытым: это сорвавшийся день или
    площадка пропала давно.
    """
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "sync_admin-20261001-120000.db").write_bytes(b"x")
    (mirror / "sync_admin-20261002-120000.db").write_bytes(b"x")

    out = _run(base, tmp_path, BACKUP_MIRROR_DIR=str(mirror))

    assert "ПАПКА-ЗЕРКАЛО" in out
    assert "копий 2" in out
    assert "ВТОРОЙ ПЛОЩАДКИ НЕТ ВОВСЕ" not in out


def test_the_job_s_own_complaint_is_printed(base, tmp_path):
    """Оговорка задания — часть ответа, а не отдельная страница.

    Разойдись они, человек сверял бы вывод скрипта с «Диагностикой» глазами.
    """
    out = _run(base, tmp_path)

    assert "Ресурс недоступен" in out
    assert "последняя удачная выгрузка: 2 дн назад" in out


def test_the_missing_mark_is_not_guessed(base, tmp_path, monkeypatch):
    """Отметки нет — так и сказано, а не «0 мин назад».

    Выдуманная давность хуже отсутствующей: по ней решают, сбой это или
    состояние.
    """
    engine = create_engine(base)
    session = sessionmaker(bind=engine)()
    session.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "backup_mirror_ok").delete()
    session.commit()
    session.close()
    engine.dispose()

    out = _run(base, tmp_path)

    assert "не было ни разу" in out


# ---------------------------------------------------------- тип облака

def _probe_module():
    """Импортировать сам скрипт как модуль.

    Здесь — В ОТЛИЧИЕ от остальных тестов файла — проверка идёт в процессе, а
    не подпроцессом, и причина платформенная: чтобы пройти по этой ветке,
    подпроцессу нужен подставной `rclone.exe`. Исполняемая заглушка — это либо
    `#!/bin/sh` (не запустится на боевом Windows), либо `.bat` (не запустится
    здесь), то есть тест был бы зелёным на одной машине и красным на другой —
    ровно то, чем останавливается накат. Предмет при этом сохраняется: мы
    проверяем НАПЕЧАТАННУЮ строку, а не то, что вернула функция.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("probe_mirror", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_s3_does_not_answer_about_and_that_is_not_a_refusal(db, monkeypatch,
                                                            capsys, tmp_path):
    """`about` умеет Яндекс.Диск, а S3 — нет, и это НЕ отказ доступа.

    Отдельный вопрос про корень оставлен не зря: 12.10 разбор развело именно
    то, что корень отвечал, а папка нет. Но с переездом на Selectel S3 тот же
    вызов отказывает ВСЕГДА — и строка «доступ к облаку: ОТКАЗ» висела бы на
    полностью исправной системе, в скрипте, который открывают, когда уже беда.
    Проверка, кричащая на исправном, кончается тем, что её перестают читать.
    """
    probe = _probe_module()
    monkeypatch.setenv("BACKUP_RCLONE_REMOTE", "selectel:awer-backups/sync_admin")
    monkeypatch.setenv("BACKUP_RCLONE_EXE", str(tmp_path / "rclone.exe"))
    monkeypatch.setenv("BACKUP_RCLONE_CONFIG", str(tmp_path / "rclone.conf"))

    def answer(exe, config, args, timeout=None):
        if args[0] == "about":
            return False, ("rclone вернул 1: Error: S3 bucket awer-backups "
                           "path sync_admin doesn't support about")
        return True, '[{"Name": "sync_admin-20261016-030000.db", "Size": 10}]'

    monkeypatch.setattr(probe, "_run_rclone", answer)

    probe._cloud(db)
    out = capsys.readouterr().out

    assert "доступ к облаку: ОТКАЗ" not in out, (
        "S3 про свободное место не отвечает вовсе — назвав это отказом, скрипт "
        "ругался бы на исправную площадку:\n" + out)
    assert "не спросить" in out, "и умолчать об этом тоже нельзя"
    assert "копий 1" in out, "а про папку скрипт обязан ответить как обычно"


def test_a_real_refusal_of_the_root_is_still_a_refusal(db, monkeypatch, capsys,
                                                       tmp_path):
    """Обратная сторона: послабление не должно глушить настоящий отказ.

    Без этой половины «не спросить» однажды накрыло бы и отозванный ключ — то
    есть ровно тот случай, ради которого вопрос про корень и задаётся.
    """
    probe = _probe_module()
    monkeypatch.setenv("BACKUP_RCLONE_REMOTE", "selectel:awer-backups/sync_admin")
    monkeypatch.setenv("BACKUP_RCLONE_EXE", str(tmp_path / "rclone.exe"))
    monkeypatch.setenv("BACKUP_RCLONE_CONFIG", str(tmp_path / "rclone.conf"))

    monkeypatch.setattr(probe, "_run_rclone", lambda *a, **kw: (
        False, "rclone вернул 1: https response error StatusCode: 403, "
               "api error SignatureDoesNotMatch"))

    probe._cloud(db)
    out = capsys.readouterr().out

    assert "доступ к облаку: ОТКАЗ" in out
    assert "БАЛАНС" in out, "причина обязана быть названа, а не только код"
