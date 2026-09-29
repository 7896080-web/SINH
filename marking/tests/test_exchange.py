"""Транспорт обмена с 1С по SFTP (ТЗ, 9.1).

Настоящего SSH-сервера в тестах нет, поэтому SFTP подменён объектом с тем же
набором методов, что у paramiko.SFTPClient, поверх временной папки — «диска
сервера». Проверяется НАША логика: что задание появляется целым под финальным
именем, что ответы разбираются и уезжают в архив сервера с копией у себя, и что
при отсутствии заданий к серверу не обращаются вовсе.
"""
import os
from pathlib import Path

import pytest

from markapp import exchange, onec, settings
from markapp.models import OnecTask


class FakeSftp:
    """Подмножество paramiko.SFTPClient поверх папки; пути — как у сервера."""

    def __init__(self, root: Path):
        self.root = root
        self.calls = []

    def _local(self, p):
        return self.root / p.lstrip("/")

    def open(self, path, mode="r"):
        self.calls.append(("open", path, mode))
        if "w" in mode:
            self._local(path).parent.mkdir(parents=True, exist_ok=True)
        return open(self._local(path), mode)

    def rename(self, a, b):
        self.calls.append(("rename", a, b))
        if self._local(b).exists():
            raise OSError("exists")        # как у sshd на Windows
        self._local(b).parent.mkdir(parents=True, exist_ok=True)
        os.rename(self._local(a), self._local(b))

    def stat(self, path):
        if not self._local(path).exists():
            raise FileNotFoundError(path)
        return os.stat(self._local(path))

    def listdir(self, path):
        d = self._local(path)
        if not d.exists():
            raise FileNotFoundError(path)
        return os.listdir(d)


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "server_C_sync"
    for d in ("tasks", "results/marking", "archive/marking"):
        (root / d).mkdir(parents=True)
    sftp = FakeSftp(root)
    connects = []

    def connect():
        connects.append(1)
        return sftp, lambda: None

    ex = exchange.SftpExchange("host", 443, "marking_sftp", "k", "kh", "/tasks", "/results/marking",
                               "/archive/marking", tmp_path / "local_archive", connect=connect)
    return root, sftp, ex, connects, tmp_path / "local_archive"


def test_task_appears_whole_under_its_final_name(db, server):
    root, sftp, ex, _, _ = server
    settings.put(db, onec.EPF_VERSION, "mark-1")
    onec.enqueue_ping(db)
    db.commit()
    with ex:
        assert onec.publish_pending(db, ex) == 1
    files = list((root / "tasks").iterdir())
    assert len(files) == 1 and files[0].name.startswith("task_mark_") and files[0].suffix == ".txt"
    # Сначала .part, потом переименование — 1С не увидит недописанное.
    opened = [c for c in sftp.calls if c[0] == "open"][0]
    renamed = [c for c in sftp.calls if c[0] == "rename"][0]
    assert opened[1].endswith(".txt.part") and renamed[2] == "/tasks/" + files[0].name
    assert db.query(OnecTask).one().status == "sent"


def test_answers_are_applied_and_archived_on_the_server_and_here(db, server):
    root, _, ex, _, local_archive = server
    task = onec.enqueue_ping(db)
    db.commit()
    with ex:
        onec.publish_pending(db, ex)
    (root / "results/marking" / "result_mark_1.txt").write_text(
        f"{task.order_id}|OK|mark-1|PING", encoding="utf-8-sig")
    # Чужой файл рядом и .part — не наши ответы.
    (root / "results/marking" / "result_mark_2.txt.part").write_text("x", encoding="utf-8")
    with ex:
        stats = onec.collect_results(db, ex)
    assert stats["files"] == 1 and onec.epf_ready(db)
    assert not (root / "results/marking" / "result_mark_1.txt").exists()
    assert (root / "archive/marking" / "result_mark_1.txt").exists()
    assert (local_archive / "result_mark_1.txt").exists()


def test_no_tasks_means_no_connection(db, monkeypatch):
    """Обращения к 1С — по факту работы с поставкой: без заданий сервер не трогаем."""
    from markapp.workers import scheduler
    opened = []

    class Boom:
        def __enter__(self):
            opened.append(1)
            raise AssertionError("к серверу обращаться не должны")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(exchange, "current", lambda: Boom())
    scheduler.job_onec_exchange()
    assert opened == []
    assert not onec.has_work(db)
    onec.enqueue_ping(db)
    db.commit()
    assert onec.has_work(db)


def test_unknown_server_key_is_refused(tmp_path, monkeypatch):
    """Без файла ключей сервера подключения нет вовсе — а не «доверять первому»."""
    ex = exchange.SftpExchange("h", 443, "u", str(tmp_path / "key"), str(tmp_path / "nope"),
                               "/tasks", "/results/marking", "/archive/marking", tmp_path)
    with pytest.raises(exchange.ExchangeError, match="нет файла ключей сервера"):
        ex.__enter__()


def test_real_client_rejects_unknown_host_keys(tmp_path, monkeypatch):
    import paramiko
    seen = {}

    class FakeClient:
        def load_host_keys(self, p):
            seen["known_hosts"] = p

        def set_missing_host_key_policy(self, policy):
            seen["policy"] = policy

        def connect(self, host, **kw):
            seen["connect"] = (host, kw)

        def open_sftp(self):
            return object()

        def close(self):
            pass
    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    kh = tmp_path / "known_hosts"
    kh.write_text("")
    ex = exchange.SftpExchange("srv", 443, "marking_sftp", "k", str(kh), "/tasks",
                               "/results/marking", "/archive/marking", tmp_path)
    with ex:
        pass
    assert isinstance(seen["policy"], paramiko.RejectPolicy)
    host, kw = seen["connect"]
    assert host == "srv" and kw["port"] == 443 and kw["username"] == "marking_sftp"
    assert kw["look_for_keys"] is False and kw["allow_agent"] is False
