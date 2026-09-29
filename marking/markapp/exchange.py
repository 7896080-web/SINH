"""Транспорт обмена с 1С: локальные папки или SFTP до сервера (ТЗ, 9.1).

Программа работает на машине с КриптоПро, а 1С и её обработка — на сервере.
Протокол обмена от этого не меняется ни на байт: задание кладётся в
`C:\\sync\\tasks`, ответы забираются из `C:\\sync\\results\\marking`. Меняется
только, КАК файл туда попадает.

Почему SFTP, а не FTP. На сервер ставится OpenSSH Server (Microsoft) на порт
443 — единственное, что для программы ставится на сервер: 443 от машины с
КриптоПро проходит (WireGuard провайдер резал). FTP — это порт
21 плюс диапазон портов данных и пароль открытым текстом. SFTP — тот же 443,
вход только по ключу, учётная запись заперта в `C:\\sync` (ChrootDirectory) и
ничего, кроме передачи файлов, не умеет (ForceCommand internal-sftp).

Правила, общие для обоих транспортов:
- задание появляется под финальным именем ЦЕЛЫМ: пишем `.part` и
  переименовываем (1С берёт `task_*.txt`, `.part` она не видит);
- ответ уходит в архив ПОСЛЕ того, как его разбор закоммичен (`onec.py`).

Ключ сервера проверяется по `known_hosts` и НЕ принимается вслепую: подмена
сервера означала бы, что наши задания (перемещения товара!) читает чужой, а
ответы «1С» пишет он же.
"""
from __future__ import annotations

import os
import posixpath
from pathlib import Path

from markapp import config

RESULT_PREFIX = "result_mark_"
CHECK_PREFIX = "supplycheck_"


class ExchangeError(RuntimeError):
    pass


class LocalExchange:
    """Папки на этой же машине — вариант «программа на сервере» и тесты."""

    name = "локальные папки"

    def __init__(self, tasks: Path, results: Path, archive: Path):
        self.tasks, self.results, self.archive = Path(tasks), Path(results), Path(archive)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put_task(self, name: str, text: str) -> None:
        self.tasks.mkdir(parents=True, exist_ok=True)
        tmp = self.tasks / (name + ".part")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.tasks / name)

    def task_exists(self, name: str) -> bool:
        return (self.tasks / name).exists()

    def result_names(self) -> list[str]:
        if not self.results.exists():
            return []
        return sorted(p.name for p in self.results.glob(f"{RESULT_PREFIX}*.txt"))

    def read_result(self, name: str) -> str | None:
        p = self.results / name
        return p.read_text(encoding="utf-8-sig") if p.exists() else None

    def archive_result(self, name: str) -> None:
        p = self.results / name
        if p.exists():
            self.archive.mkdir(parents=True, exist_ok=True)
            os.replace(p, self.archive / name)


class SftpExchange:
    """Папки сервера по SFTP. Соединение — на один проход обмена, не постоянное:
    к 1С обращаются, только когда есть задание, ждущее отправки или ответа."""

    name = "SFTP"

    def __init__(self, host: str, port: int, user: str, key_path: str, known_hosts: str,
                 tasks: str, results: str, archive: str, local_archive: Path,
                 connect=None):
        self.host, self.port, self.user = host, port, user
        self.key_path, self.known_hosts = key_path, known_hosts
        self.tasks, self.results, self.archive = tasks, results, archive
        self.local_archive = Path(local_archive)
        self._connect = connect or self._paramiko_connect
        self._sftp = None
        self._close = None

    def _paramiko_connect(self):
        import paramiko
        if not Path(self.known_hosts).exists():
            raise ExchangeError(f"нет файла ключей сервера {self.known_hosts} — "
                                "первое подключение по инструкции deploy/SFTP_1C.md")
        client = paramiko.SSHClient()
        client.load_host_keys(self.known_hosts)
        # Незнакомый ключ сервера — отказ, а не «запомнить»: см. докстринг модуля.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(self.host, port=self.port, username=self.user,
                           key_filename=self.key_path, look_for_keys=False,
                           allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
        except paramiko.BadHostKeyException as e:
            raise ExchangeError("КЛЮЧ СЕРВЕРА ИЗМЕНИЛСЯ — обмен остановлен. Это либо "
                                "переустановка SSH на сервере, либо подмена. Разберитесь, "
                                f"прежде чем править known_hosts: {e}") from e
        except paramiko.SSHException as e:
            raise ExchangeError(f"SFTP {self.host}:{self.port}: {e}") from e
        except OSError as e:
            raise ExchangeError(f"SFTP {self.host}:{self.port} недоступен: {e}") from e
        return client.open_sftp(), client.close

    def __enter__(self):
        self._sftp, self._close = self._connect()
        return self

    def __exit__(self, *exc):
        try:
            if self._close:
                self._close()
        finally:
            self._sftp = self._close = None
        return False

    def _p(self, directory: str, name: str) -> str:
        return posixpath.join(directory, name)

    def put_task(self, name: str, text: str) -> None:
        tmp = self._p(self.tasks, name + ".part")
        with self._sftp.open(tmp, "wb") as f:
            f.write(text.encode("utf-8"))
        self._sftp.rename(tmp, self._p(self.tasks, name))

    def task_exists(self, name: str) -> bool:
        try:
            self._sftp.stat(self._p(self.tasks, name))
            return True
        except OSError:
            return False

    def result_names(self) -> list[str]:
        try:
            names = self._sftp.listdir(self.results)
        except OSError as e:
            raise ExchangeError(f"нет доступа к {self.results} на сервере: {e}") from e
        return sorted(n for n in names if n.startswith(RESULT_PREFIX) and n.endswith(".txt"))

    def read_result(self, name: str) -> str | None:
        try:
            with self._sftp.open(self._p(self.results, name), "rb") as f:
                data = f.read()
        except OSError:
            return None
        return data.decode("utf-8-sig")

    def archive_result(self, name: str) -> None:
        src = self._p(self.results, name)
        try:
            with self._sftp.open(src, "rb") as f:
                data = f.read()
        except OSError:
            return
        # Копия и у себя: история обмена должна быть там, где программа, — на
        # сервер человек с этой машины не ходит.
        self.local_archive.mkdir(parents=True, exist_ok=True)
        (self.local_archive / name).write_bytes(data)
        self._sftp.rename(src, self._p(self.archive, name))


def current():
    """Транспорт по настройкам: задан хост SFTP — SFTP, иначе локальные папки."""
    if config.ONEC_SFTP_HOST:
        return SftpExchange(config.ONEC_SFTP_HOST, config.ONEC_SFTP_PORT, config.ONEC_SFTP_USER,
                            config.ONEC_SFTP_KEY, config.ONEC_SFTP_KNOWN_HOSTS,
                            config.ONEC_SFTP_TASKS, config.ONEC_SFTP_RESULTS,
                            config.ONEC_SFTP_ARCHIVE, config.ONEC_ARCHIVE_DIR)
    return LocalExchange(config.ONEC_TASKS_DIR, config.ONEC_RESULTS_DIR, config.ONEC_ARCHIVE_DIR)
