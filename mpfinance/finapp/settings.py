"""Настройки «ключ — значение».

Значения по умолчанию здесь не заводятся в базу: `get` отдаёт умолчание, пока
человек его не поменял. Настройки расчёта (налог, склады 1С, курс) разбирает в
типы `finance/settings.py`.
"""
from sqlalchemy.orm import Session

from finapp.models import Setting

# Обмен с 1С включается человеком на «Диагностике» — последним этапом, когда
# программа проверена, а модуль обработки обновлён до mark-4. Выключен — в папки
# обмена C:\sync программа не пишет и не заглядывает.
ONEC_ENABLED = "onec_enabled"
# Обработка 1С отвечает на FIN_PING своей версией («mark-4»). До ответа версии
# не ниже 4 никакие другие команды не шлём — см. onec.py.
ONEC_EPF_VERSION = "onec_epf_version"
ONEC_EPF_CHECKED_AT = "onec_epf_checked_at"
CATALOG_LOADED_AT = "onec_catalog_loaded_at"
CATALOG_ROWS = "onec_catalog_rows"
COSTS_LOADED_AT = "onec_costs_loaded_at"
SYNC_CATALOG_AT = "sync_catalog_loaded_at"
# Вторая площадка копий: путь в облаке (remote:бакет/папка), итог последней
# выгрузки и время последней УДАЧНОЙ — по ней видно, сколько суток площадки нет.
BACKUP_REMOTE = "backup_remote"
BACKUP_REMOTE_RESULT = "backup_remote_result"
BACKUP_REMOTE_OK_AT = "backup_remote_ok_at"
# Тарифы WB по категориям (`finance/tariffs.py`): итог последней загрузки и время
# последней УДАЧНОЙ — суточное задание по нему решает, пора ли спрашивать снова.
TARIFFS_RESULT = "wb_tariffs_result"
TARIFFS_OK_AT = "wb_tariffs_ok_at"


def get(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row is not None else default


def put(db: Session, key: str, value: str) -> bool:
    """Записать (без коммита). True — значение изменилось."""
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
        db.flush()
        return True
    if row.value == value:
        return False
    row.value = value
    return True
