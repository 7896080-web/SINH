"""Настройки программы из окружения (то, что зависит от машины)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# Обмен с 1С. Папка заданий ОБЩАЯ с sync_admin, «Маркировкой» и «Репрайсером»
# (обработка берёт все task_*.txt), ответы — в СВОЁМ подкаталоге results\finance:
# обработка (mark-4) кладёт туда ответы на задания task_fin_*.txt. sync_admin
# забирает из results все result_*.txt без захода в подкаталоги — ответ, лёгший
# рядом с его ответами, он унёс бы в архив как чужой.
ONEC_TASKS_DIR = _path("MPFIN_ONEC_TASKS_DIR", r"C:\sync\tasks")
ONEC_RESULTS_DIR = _path("MPFIN_ONEC_RESULTS_DIR", r"C:\sync\results\finance")
ONEC_ARCHIVE_DIR = _path("MPFIN_ONEC_ARCHIVE_DIR", r"C:\sync\archive\finance")

# Через сколько минут без ответа задание 1С считается зависшим. Обработка
# работает по своему расписанию, а срез себестоимости и справочник баркодов —
# тяжёлые запросы; документ продаж на сотни строк 1С тоже пишет не мгновенно.
ONEC_TIMEOUT_MINUTES = int(os.environ.get("MPFIN_ONEC_TIMEOUT_MINUTES", "60"))

# База sync_admin — ТОЛЬКО ЧТЕНИЕ, для сверки отчёта площадки с принятыми
# заказами. Пусто — сверка заказов выключена, остальное работает.
SYNC_DB_PATH = os.environ.get("MPFIN_SYNC_DB", r"C:\sync_admin\sync_admin.db")

BACKUP_DIR = _path("MPFIN_BACKUP_DIR", str(BASE_DIR / "backups"))
