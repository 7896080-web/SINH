"""Настройки программы «ключ — значение» и значения по умолчанию.

`ensure_defaults` идемпотентна: её зовут веб и воркер при старте. Она
заводит только отсутствующее и никогда не перезаписывает то, что человек
поменял на странице.
"""
from sqlalchemy.orm import Session

from markapp.models import Organization, Setting

LAMODA_ORG = "lamoda_org_id"
SUPPLY_LAST_NUMBER = "supply_last_number"
SUPPLY_STEP = "supply_step"
AGENCY_FROM = "agency_from"          # дата перехода на агентский договор, ДД.ММ.ГГГГ

DEFAULTS = {
    SUPPLY_LAST_NUMBER: "12550",
    SUPPLY_STEP: "10",
    AGENCY_FROM: "01.10.2026",
}

# Реквизиты ИП Яворской — из upd-constructor (источник истины — принятые УПД).
YAVORSKAYA = dict(
    name="ИП Яворская Т.Н.", surname="Яворская", firstname="Татьяна", patronymic="Никитовна",
    inn="910223073620", ogrnip="320911200051564",
    address="295018, Крым, Симферополь, Ракетная, д. 33", signer_role="ИП", vat_rate=5,
    contract_number="б/н", sticker_sender="Отправитель: ИП Яворская Т.Н",
    edo_sender_id="2BM-910223073620-20211210091210985126400000000",
)


def get(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, "")


def put(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def lamoda_org(db: Session) -> Organization | None:
    raw = get(db, LAMODA_ORG)
    return db.get(Organization, int(raw)) if raw.isdigit() else None


def ensure_defaults(db: Session) -> None:
    org = db.query(Organization).filter(Organization.inn == YAVORSKAYA["inn"]).first()
    if org is None and db.query(Organization).count() == 0:
        org = Organization(**YAVORSKAYA)
        db.add(org)
        db.flush()
    if db.get(Setting, LAMODA_ORG) is None and org is not None:
        db.add(Setting(key=LAMODA_ORG, value=str(org.id)))
    for key, value in DEFAULTS.items():
        if db.get(Setting, key) is None:
            db.add(Setting(key=key, value=value))
    db.commit()
