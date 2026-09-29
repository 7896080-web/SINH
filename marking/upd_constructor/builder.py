"""Сборка XML УПД 5.03/ДОП по структуре принятых эталонов. Вывод: windows-1251, CRLF, без хвостового перевода строки."""
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal

from . import config as C
from .calc import Line, Totals

E = ET.SubElement


def _fmt_qty(q: Decimal) -> str:
    return str(int(q)) if q == q.to_integral_value() else str(q.normalize())


def _addr_rf(parent, a: C.Address):
    E(parent, "АдрРФ", {"КодРегион": a.region_code, "НаимРегион": a.region_name, "Индекс": a.index,
                        "Район": a.district, "Улица": a.street, "Дом": a.building})


def _ip_id(parent, s: C.SellerIP):
    idsv = E(parent, "ИдСв")
    ip = E(idsv, "СвИП", {"ИННФЛ": s.inn, "ОГРНИП": s.ogrnip})
    E(ip, "ФИО", {"Фамилия": s.surname, "Имя": s.firstname, "Отчество": s.patronymic})


def _ul_id(parent, b: C.Buyer):
    idsv = E(parent, "ИдСв")
    E(idsv, "СвЮЛУч", {"НаимОрг": b.org_name, "ИННЮЛ": b.inn, "КПП": b.kpp})


def make_id_file(doc_date: str, edo: C.EdoIds = C.EDO) -> str:
    ymd = "".join(reversed(doc_date.split(".")))
    return f"ON_NSCHFDOPPR_{edo.receiver}_{edo.sender}_{ymd}_{uuid.uuid4()}_0_1_0_0_0_00"


def build_upd(lines: list[Line], totals: Totals, *, doc_number: str, doc_date: str, ttn_number: str,
              ttn_date: str, transfer_date: str, contract_number: str = "б/н", contract_date: str | None = None,
              rate_label: str = "5%", seller: C.SellerIP = C.SELLER, buyer: C.Buyer = C.BUYER,
              now: datetime | None = None, id_file: str | None = None,
              scheme: C.Scheme | None = None) -> tuple[str, bytes]:
    """Возвращает (ИдФайл, байты XML в windows-1251).
    scheme=None — схема по дате УПД (C.scheme_for): по 30.09.2026 комиссия, с 01.10.2026 агентский договор."""
    now = now or datetime.now()
    scheme = scheme or C.scheme_for(doc_date)
    contract_date = contract_date or doc_date          # в обоих эталонах дата договора == дате УПД
    id_file = id_file or make_id_file(doc_date)

    root = ET.Element("Файл", {"ИдФайл": id_file, "ВерсФорм": C.FORMAT_VERSION, "ВерсПрог": C.EDO.soft_name})
    doc = E(root, "Документ", {"КНД": "1115131", "Функция": "ДОП", "ПоФактХЖ": C.DOC_TITLE,
                               "НаимДокОпр": C.DOC_TITLE, "ДатаИнфПр": now.strftime("%d.%m.%Y"),
                               "ВремИнфПр": now.strftime("%H.%M.%S")})
    sf = E(doc, "СвСчФакт", {"НомерДок": doc_number, "ДатаДок": doc_date})

    prod = E(sf, "СвПрод"); _ip_id(prod, seller)
    E(E(prod, "Адрес"), "АдрИнф", {"НаимСтран": "РОССИЯ", "КодСтр": "643", "АдрТекст": seller.address})

    gr = E(E(sf, "ГрузОт"), "ГрузОтпр"); _ip_id(gr, seller)
    E(E(gr, "Адрес"), "АдрИнф", {"НаимСтран": "РОССИЯ", "КодСтр": "643", "АдрТекст": seller.address})

    gp = E(sf, "ГрузПолуч", {"ОКПО": buyer.okpo}); _ul_id(gp, buyer)
    _addr_rf(E(gp, "Адрес"), buyer.consignee_address)

    E(sf, "ДокПодтвОтгрНом", {"РеквНаимДок": "ТТН", "РеквНомерДок": ttn_number, "РеквДатаДок": ttn_date})

    pk = E(sf, "СвПокуп", {"ОКПО": buyer.okpo}); _ul_id(pk, buyer)
    _addr_rf(E(pk, "Адрес"), buyer.legal_address)

    E(sf, "ДенИзм", {"КодОКВ": "643", "НаимОКВ": "Российский рубль"})
    E(sf, "ДопСвФХЖ1", {"СпОбстФДОП": "00005"})

    tab = E(doc, "ТаблСчФакт")
    for i, l in enumerate(lines, 1):
        it = E(tab, "СведТов", {"НомСтр": str(i), "НаимТов": l.name, "ОКЕИ_Тов": "796", "НаимЕдИзм": "шт",
                                "КолТов": _fmt_qty(l.qty), "ЦенаТов": f"{l.unit_bez:.2f}",
                                "СтТовБезНДС": f"{l.bez:.2f}", "НалСт": rate_label, "СтТовУчНал": f"{l.uch:.2f}"})
        ds = E(it, "ДопСведТов", {"КодТов": l.gtin})
        E(E(ds, "НомСредИдентТов"), "КИЗ").text = l.kiz
        E(E(it, "Акциз"), "БезАкциз").text = "без акциза"
        E(E(it, "СумНал"), "СумНал").text = f"{l.nal:.2f}"
    vs = E(tab, "ВсегоОпл", {"СтТовБезНДСВсего": f"{totals.bez:.2f}", "СтТовУчНалВсего": f"{totals.uch:.2f}",
                             "КолНеттоВс": f"{totals.qty:.3f}"})
    E(E(vs, "СумНалВсего"), "СумНал").text = f"{totals.nal:.2f}"

    pp = E(doc, "СвПродПер")
    sp = E(pp, "СвПер", {"СодОпер": "Товары переданы", "ВидОпер": scheme.vid_oper, "ДатаПер": transfer_date})
    E(sp, "ОснПер", {"РеквНаимДок": scheme.basis_name, "РеквНомерДок": contract_number, "РеквДатаДок": contract_date})

    sg = E(doc, "Подписант", {"СпосПодтПолном": "1", "Должн": seller.signer_role})
    E(sg, "ФИО", {"Фамилия": seller.surname.upper(), "Имя": seller.firstname.upper(),
                  "Отчество": seller.patronymic.upper()})

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    text = '<?xml version="1.0" encoding="windows-1251"?>\r\n' + body.replace("\n", "\r\n")
    return id_file, text.encode("cp1251", errors="xmlcharrefreplace")
