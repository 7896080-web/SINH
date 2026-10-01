"""Настройки программы из окружения (то, что зависит от машины)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# Обмен с 1С. Папка заданий ОБЩАЯ с sync_admin и «Маркировкой» (обработка берёт
# все task_*.txt), ответы — в СВОЁМ подкаталоге results\pricing: обработка
# (часть mark-3) кладёт туда ответы на задания task_price_*.txt. sync_admin
# забирает из results все result_*.txt без захода в подкаталоги — ответ, лёгший
# рядом с его ответами, он унёс бы в архив как чужой.
ONEC_TASKS_DIR = _path("REPRICER_ONEC_TASKS_DIR", r"C:\sync\tasks")
ONEC_RESULTS_DIR = _path("REPRICER_ONEC_RESULTS_DIR", r"C:\sync\results\pricing")
ONEC_ARCHIVE_DIR = _path("REPRICER_ONEC_ARCHIVE_DIR", r"C:\sync\archive\pricing")

# Программа на офисном компьютере, 1С — на сервере: папки выше — сервера, и до
# них ходим по SFTP на 443 (как «Маркировка», её deploy/SFTP_1C.md). Хост не
# задан — локальные папки (тесты и запасной вариант «на сервере»).
ONEC_SFTP_HOST = os.environ.get("REPRICER_ONEC_SFTP_HOST", "")
ONEC_SFTP_PORT = int(os.environ.get("REPRICER_ONEC_SFTP_PORT", "443"))
# Учётная запись на сервере заперта в C:\sync и умеет только SFTP. Можно
# использовать ту же, что у «Маркировки», дописав на сервере второй ключ.
ONEC_SFTP_USER = os.environ.get("REPRICER_ONEC_SFTP_USER", "marking_sftp")
ONEC_SFTP_KEY = os.environ.get("REPRICER_ONEC_SFTP_KEY", str(BASE_DIR / "ssh" / "id_ed25519"))
ONEC_SFTP_KNOWN_HOSTS = os.environ.get("REPRICER_ONEC_SFTP_KNOWN_HOSTS",
                                       str(BASE_DIR / "ssh" / "known_hosts"))
ONEC_SFTP_TASKS = os.environ.get("REPRICER_ONEC_SFTP_TASKS", "/tasks")
ONEC_SFTP_RESULTS = os.environ.get("REPRICER_ONEC_SFTP_RESULTS", "/results/pricing")
ONEC_SFTP_ARCHIVE = os.environ.get("REPRICER_ONEC_SFTP_ARCHIVE", "/archive/pricing")

# Через сколько минут без ответа задание 1С считается зависшим.
ONEC_TIMEOUT_MINUTES = int(os.environ.get("REPRICER_ONEC_TIMEOUT_MINUTES", "20"))

BACKUP_DIR = _path("REPRICER_BACKUP_DIR", str(BASE_DIR / "backups"))

# Курс ЦБ РФ — официальный ежедневный XML, без ключа.
CBR_URL = os.environ.get("REPRICER_CBR_URL", "https://www.cbr.ru/scripts/XML_daily.asp")
