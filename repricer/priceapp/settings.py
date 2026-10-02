"""Настройки «ключ — значение» и значения по умолчанию.

`ensure_defaults` идемпотентна: заводит только отсутствующее и никогда не
перезаписывает то, что человек поменял на странице.
"""
from sqlalchemy.orm import Session

from priceapp.models import Setting

RATE_MODE = "rate_mode"              # cbr — курс ЦБ на сегодня; manual — введённый руками
RATE_MANUAL = "rate_manual"          # ручной курс, ₽ за $1
# Обработка 1С умеет задания репрайсера (часть mark-3): ставится по ПЕРВОМУ
# ответу OK на EXPORT_COST_PRICES. До этого другие команды не шлём — см. onec.py.
EPF_READY_AT = "onec_epf_ready_at"
COST_LOADED_AT = "onec_cost_loaded_at"
COST_ROWS = "onec_cost_rows"
DICT_LOADED_AT = "onec_dict_loaded_at"
DICT_ROWS = "onec_dict_rows"
# Курс ушёл от курса последнего расчёта больше чем на столько % — предупреждаем.
RATE_ALERT_PERCENT = "rate_alert_percent"

DEFAULTS = {
    RATE_MODE: "cbr",
    RATE_MANUAL: "",
    RATE_ALERT_PERCENT: "2",
}


def get(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, "")


def put(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
        db.flush()
    else:
        row.value = value


def ensure_defaults(db: Session) -> None:
    for key, value in DEFAULTS.items():
        if db.get(Setting, key) is None:
            db.add(Setting(key=key, value=value))
    db.commit()
