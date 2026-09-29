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

# Через сколько минут без ответа задание 1С считается зависшим. Обработка
# запускается по расписанию; штатный ответ у sync_admin приходит за 4-7 минут.
ONEC_TIMEOUT_MINUTES = int(os.environ.get("MARKING_ONEC_TIMEOUT_MINUTES", "15"))

# Имена складов в 1С (п. 6.2 ТЗ).
ONEC_WAREHOUSE_FROM = os.environ.get("MARKING_WAREHOUSE_FROM", "ЦС Склад")
ONEC_WAREHOUSE_TO = os.environ.get("MARKING_WAREHOUSE_TO", "Lamoda_Склад")

BACKUP_DIR = _path("MARKING_BACKUP_DIR", str(BASE_DIR / "backups"))
