"""Фиксированные реквизиты. Источник истины — принятые покупателем эталонные УПД (fixtures/reference)."""
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class SellerIP:
    surname: str = "Яворская"
    firstname: str = "Татьяна"
    patronymic: str = "Никитовна"
    inn: str = "910223073620"
    ogrnip: str = "320911200051564"
    address: str = "295018, Крым, Симферополь, Ракетная, д. 33"
    signer_role: str = "ИП"


@dataclass(frozen=True)
class Address:
    region_code: str
    region_name: str
    index: str
    district: str
    street: str
    building: str


@dataclass(frozen=True)
class Buyer:
    org_name: str = 'Общество с ограниченной ответственностью "Купишуз"'
    inn: str = "7705935687"
    kpp: str = "773101001"
    okpo: str = "69597891"
    # ГрузПолуч — склад. СвПокуп — юрлицо. Адреса РАЗНЫЕ (подтверждено обоими эталонами).
    consignee_address: Address = Address("50", "Московская область", "140150",
                                         "Раменский городской округ", "Логистический технопарк Софьино", "с5/1")
    legal_address: Address = Address("77", "г. Москва", "121614",
                                     "вн. тер. г. муниципальный округ Крылатское", "Крылатская", "15")


@dataclass(frozen=True)
class EdoIds:
    """Идентификаторы участников в ИдФайл (как в эталонах, оператор Контур/Диадок = префикс 2BM).
    НЕ проверено на приёме оператором — см. CLAUDE.md, открытые вопросы."""
    receiver: str = "2BM-7705935687-772601001-201312110824543397696"
    sender: str = "2BM-910223073620-20211210091210985126400000000"
    soft_name: str = "UPD-Constructor"  # ВерсПрог; в эталонах стоит "LinenMark" (ПО, которым они созданы)


SELLER = SellerIP()
BUYER = Buyer()
EDO = EdoIds()
FORMAT_VERSION = "5.03"
DOC_TITLE = ("Документ об отгрузке товаров (выполнении работ), передаче имущественных прав "
             "(документ об оказании услуг)")


@dataclass(frozen=True)
class Scheme:
    """Схема договора с Lamoda: что пишется в СвПер/@ВидОпер и ОснПер/@РеквНаимДок.
    Всё остальное в УПД (СпОбстФДОП=00005, функция ДОП, коды, суммы, реквизиты) от схемы не зависит."""
    name: str
    vid_oper: str
    basis_name: str


# По 30.09.2026 — договор комиссии (так в обоих принятых эталонах).
COMMISSION = Scheme("commission", "ПродажаКомиссия", "Договор комиссии")
# С 01.10.2026 Lamoda переводит FBO на агентскую модель (статья Lamoda от 16.09.2026). У Lamoda техническая
# проверка: в ВидОпер недопустимы «ПродажаКомиссия» и «Комиссия», в НаимОсн/НомОсн/Значен — «комис%».
# ВидОпер — значение без «комис» по совету Lamoda («Агент» / «Агентская продажа» / «Реализация по агентскому
# договору»); выбрано последнее. НЕ подтверждено принятым документом — при первом принятом УПД сверить.
AGENCY = Scheme("agency", "Реализация по агентскому договору", "Агентский договор")
SCHEMES = {s.name: s for s in (COMMISSION, AGENCY)}
# Схема выбирается по ДАТЕ УПД («документы, датированные 30 сентября и ранее, формируются по старым правилам»).
AGENCY_FROM = date(2026, 10, 1)


def _ru(d: str) -> date:
    dd, mm, yy = d.split(".")
    return date(int(yy), int(mm), int(dd))


def scheme_for(doc_date: str) -> Scheme:
    """Схема по дате УПД в формате ДД.ММ.ГГГГ."""
    return AGENCY if _ru(doc_date) >= AGENCY_FROM else COMMISSION
