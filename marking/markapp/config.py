"""Настройки программы из окружения.

Всё, что зависит от машины (пути обмена с 1С, каталог копий), — здесь, а не
константами по коду: на сервере это `C:\\sync\\…`, в тестах — временные папки.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# Обмен с 1С. Папка заданий ОБЩАЯ с sync_admin (обработка 1С берёт из неё все
# task_*.txt), ответы — в СВОЁМ подкаталоге results\marking: sync_admin
# забирает из results все result_*.txt без захода в подкаталоги, и ответ на
# наше задание, положенный рядом с его ответами, он унёс бы в архив как чужой.
ONEC_TASKS_DIR = _path("MARKING_ONEC_TASKS_DIR", r"C:\sync\tasks")
ONEC_RESULTS_DIR = _path("MARKING_ONEC_RESULTS_DIR", r"C:\sync\results\marking")
ONEC_ARCHIVE_DIR = _path("MARKING_ONEC_ARCHIVE_DIR", r"C:\sync\archive\marking")

# Программа стоит на машине с КриптоПро, 1С — на сервере: папки выше тогда
# сервера, а не этой машины, и до них ходим по SFTP (`exchange.py`, ТЗ 9.1).
# Хост не задан — папки локальные (вариант «на сервере» и тесты).
ONEC_SFTP_HOST = os.environ.get("MARKING_ONEC_SFTP_HOST", "")
ONEC_SFTP_PORT = int(os.environ.get("MARKING_ONEC_SFTP_PORT", "443"))
ONEC_SFTP_USER = os.environ.get("MARKING_ONEC_SFTP_USER", "marking_sftp")
ONEC_SFTP_KEY = os.environ.get("MARKING_ONEC_SFTP_KEY", str(BASE_DIR / "ssh" / "id_ed25519"))
ONEC_SFTP_KNOWN_HOSTS = os.environ.get("MARKING_ONEC_SFTP_KNOWN_HOSTS",
                                       str(BASE_DIR / "ssh" / "known_hosts"))
# Пути внутри SFTP: учётная запись заперта в C:\sync (ChrootDirectory).
ONEC_SFTP_TASKS = os.environ.get("MARKING_ONEC_SFTP_TASKS", "/tasks")
ONEC_SFTP_RESULTS = os.environ.get("MARKING_ONEC_SFTP_RESULTS", "/results/marking")
ONEC_SFTP_ARCHIVE = os.environ.get("MARKING_ONEC_SFTP_ARCHIVE", "/archive/marking")

# Через сколько минут без ответа задание 1С считается зависшим. Обработка
# запускается по расписанию; штатный ответ у sync_admin приходит за 4-7 минут.
ONEC_TIMEOUT_MINUTES = int(os.environ.get("MARKING_ONEC_TIMEOUT_MINUTES", "15"))

# Имена складов в 1С (п. 6.2 ТЗ).
ONEC_WAREHOUSE_FROM = os.environ.get("MARKING_WAREHOUSE_FROM", "ЦС Склад")
ONEC_WAREHOUSE_TO = os.environ.get("MARKING_WAREHOUSE_TO", "Lamoda_Склад")

BACKUP_DIR = _path("MARKING_BACKUP_DIR", str(BASE_DIR / "backups"))
