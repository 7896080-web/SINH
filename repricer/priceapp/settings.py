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

# Эталонная цена WB (`reference.py`): включено ли правило, на сколько % падение «к получению»
# от эталонной возвращает эталонную, на сколько % сдвиг курса пересчитывает эталонную;
# отметка «опросить WB сейчас» (курс ушёл дальше порога).
REF_ENABLED = "ref_enabled"
REF_DROP_PERCENT = "ref_drop_percent"
REF_RATE_PERCENT = "ref_rate_percent"
REF_POLL_NOW = "ref_poll_now"
REF_MAX_RAISE_PERCENT = "ref_max_raise_percent"
WB_MAX_DROP_PERCENT = "wb_max_drop_percent"
FOLLOW_WB_SNAPSHOT = "follow_wb_snapshot"
REF_SNAPSHOT_HASH = "ref_snapshot_hash"

# Kit следует за WB (`follow.py`): включено ли, на сколько % цена Kit ниже средней цены WB для
# покупателя, при каком смещении средней пересчитываем наценку, до какого шага (% к последней
# отправленной цене) подтверждаем сами, и когда опросить площадки после отправки цен на WB.
DISCOUNT_GUARD_ENABLED = "discount_guard_enabled"
FOLLOW_ENABLED = "follow_enabled"
FOLLOW_PERCENT = "follow_percent"
FOLLOW_TOLERANCE = "follow_tolerance"
FOLLOW_MAX_STEP = "follow_max_step"
FOLLOW_DUE_AT = "follow_due_at"
# Площадки следуют за эталоном WB (`etalon.py`, страница «Эталонные цены»): цена после скидки
# продавца на площадке = эталон − N%; пусто — площадка за эталоном не следует. Скидка продавца —
# целая, в коридоре ETALON_DISC_MIN–MAX (ниже). Автоследование при опросе WB — отдельный выключатель.
ETALON_SHIFT_KIT = "etalon_shift_kit"
ETALON_SHIFT_LAMODA = "etalon_shift_lamoda"
ETALON_SHIFT_OZON = "etalon_shift_ozon"
ETALON_FOLLOW_AUTO = "etalon_follow_auto"
ETALON_LAMODA_SALE_DAYS = "etalon_lamoda_sale_days"
# Разница цены после скидки с целью не больше стольких рублей — не расхождение (округление, курс).
ETALON_TOLERANCE_RUB = "etalon_tolerance_rub"
# Опрос WB (цены + возврат эталона) ещё и в эти часы по местному времени — «чч:мм» через запятую.
# Акции WB стартуют в полночь: без опроса в 00:05 их цены жили бы до ближайшего опроса раз в 6 часов.
WB_POLL_AT = "wb_poll_at"
WB_POLL_SLOT_DONE = "wb_poll_slot_done"

# Эталон WB (`pricing.etalon_prices`): цена продавца со скидкой на WB минус этот %;
# допустимая скидка продавца на площадке (вне — цена до скидки не считается); «от
# эталона, %» по умолчанию площадки (пусто — выключено).
ETALON_WB_DISCOUNT = "etalon_wb_discount"
ETALON_DISC_MIN = "etalon_disc_min"
ETALON_DISC_MAX = "etalon_disc_max"
ETALON_PLATFORMS = ("kit", "lamoda", "ozon")   # площадки, которым можно задать «от эталона»
ETALON_SUGGEST = {"kit": "-10", "lamoda": "0"}  # подсказка на странице; включает человек


def etalon_pct_key(platform: str) -> str:
    return f"etalon_pct_{platform}"


# Себестоимость из 1С выравнивается по «артикул + цвет» (самое частое значение): 0 — выключить.
COST_NORMALIZE = "cost_normalize"

DEFAULTS = {
    RATE_MODE: "cbr",
    RATE_MANUAL: "",
    RATE_ALERT_PERCENT: "2",
    REF_ENABLED: "1",
    REF_DROP_PERCENT: "10",
    REF_RATE_PERCENT: "2",
    REF_POLL_NOW: "0",
    REF_MAX_RAISE_PERCENT: "50",
    WB_MAX_DROP_PERCENT: "33",
    FOLLOW_WB_SNAPSHOT: "",
    REF_SNAPSHOT_HASH: "",
    ETALON_WB_DISCOUNT: "31",
    ETALON_DISC_MIN: "35",
    ETALON_DISC_MAX: "50",
    DISCOUNT_GUARD_ENABLED: "1",
    FOLLOW_ENABLED: "1",
    FOLLOW_PERCENT: "10",
    FOLLOW_TOLERANCE: "1",
    FOLLOW_MAX_STEP: "50",
    FOLLOW_DUE_AT: "",
    ETALON_SHIFT_KIT: "45",
    ETALON_SHIFT_LAMODA: "31",
    ETALON_SHIFT_OZON: "",
    ETALON_FOLLOW_AUTO: "0",
    ETALON_LAMODA_SALE_DAYS: "365",
    ETALON_TOLERANCE_RUB: "100",
    WB_POLL_AT: "00:05",
    WB_POLL_SLOT_DONE: "",
    COST_NORMALIZE: "1",
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
