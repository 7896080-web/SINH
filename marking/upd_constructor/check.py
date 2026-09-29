"""Проверка готового УПД XML: структура, реквизиты покупателя, математика, итоги, дубли КИЗ."""
from dataclasses import dataclass
from decimal import Decimal
import xml.etree.ElementTree as ET

from . import config as C
from .calc import q2

D = Decimal


@dataclass
class Finding:
    level: str   # ERROR | WARN | INFO
    msg: str

    def __str__(self):
        return f"[{self.level}] {self.msg}"


def _addr(el):
    a = el.find("Адрес/АдрРФ")
    return dict(a.attrib) if a is not None else None


def check_file(path, expected_scheme=None) -> list[Finding]:
    """expected_scheme — схема, которую выбрали при сборке (C.COMMISSION / C.AGENCY). None — положенная по дате УПД."""
    F: list[Finding] = []
    err = lambda m: F.append(Finding("ERROR", m))
    info = lambda m: F.append(Finding("INFO", m))
    warn = lambda m: F.append(Finding("WARN", m))

    raw = open(path, "rb").read()
    head = raw[:120].decode("ascii", "ignore").lower()
    info("кодировка в декларации: " + ("windows-1251" if "windows-1251" in head else "UTF-8/другая"))
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return [Finding("ERROR", f"XML не парсится: {e}")]
    doc = root.find("Документ")
    sf = doc.find("СвСчФакт")
    if root.get("ВерсФорм") != "5.03": err("ВерсФорм != 5.03")
    if doc.get("КНД") != "1115131" or doc.get("Функция") != "ДОП": err("КНД/Функция не 1115131/ДОП")

    b = C.BUYER
    for tag in ("ГрузПолуч", "СвПокуп"):
        el = sf.find(tag)
        if el is None: err(f"нет блока {tag}"); continue
        u = el.find("ИдСв/СвЮЛУч")
        if (el.get("ОКПО"), u.get("ИННЮЛ"), u.get("КПП"), u.get("НаимОрг")) != (b.okpo, b.inn, b.kpp, b.org_name):
            err(f"{tag}: реквизиты покупателя не совпадают с эталоном")
    ga, pa = _addr(sf.find("ГрузПолуч")), _addr(sf.find("СвПокуп"))
    exp_g, exp_p = C.BUYER.consignee_address, C.BUYER.legal_address
    if ga != {"КодРегион": exp_g.region_code, "НаимРегион": exp_g.region_name, "Индекс": exp_g.index,
              "Район": exp_g.district, "Улица": exp_g.street, "Дом": exp_g.building}:
        err("ГрузПолуч: адрес не совпадает с адресом склада (Раменское)")
    if pa != {"КодРегион": exp_p.region_code, "НаимРегион": exp_p.region_name, "Индекс": exp_p.index,
              "Район": exp_p.district, "Улица": exp_p.street, "Дом": exp_p.building}:
        err("СвПокуп: адрес не совпадает с юридическим (Москва, Крылатское)")

    items = doc.findall("ТаблСчФакт/СведТов")
    info(f"позиций: {len(items)}")
    rates = {i.get("НалСт") for i in items}
    if len(rates) != 1: err(f"разные ставки НДС: {rates}")
    rate = D(next(iter(rates)).rstrip("%")) / 100 if len(rates) == 1 and next(iter(rates)).rstrip("%").isdigit() else None
    s_qty = s_bez = s_nal = s_uch = D(0)
    seen, dup, bad_rate = set(), 0, 0
    for n, it in enumerate(items, 1):
        a = it.attrib
        if int(a["НомСтр"]) != n: err(f"НомСтр {a['НомСтр']} вместо {n}")
        qty, unit, bez, uch = D(a["КолТов"]), D(a["ЦенаТов"]), D(a["СтТовБезНДС"]), D(a["СтТовУчНал"])
        nal = D(it.findtext("СумНал/СумНал"))
        if abs(unit * qty - bez) > D("0.02"): err(f"стр.{n}: ЦенаТов*Кол != СтТовБезНДС")
        if bez + nal != uch: err(f"стр.{n}: без НДС + НДС != с НДС")
        if rate is not None and abs(nal - q2(uch - uch / (1 + rate))) > D("0.01"): bad_rate += 1
        kiz = it.findtext("ДопСведТов/НомСредИдентТов/КИЗ")
        if not kiz: err(f"стр.{n}: нет КИЗ")
        elif kiz in seen: dup += 1
        seen.add(kiz)
        if not it.find("ДопСведТов").get("КодТов"): err(f"стр.{n}: нет ГТИН")
        if not a.get("НаимТов"): err(f"стр.{n}: нет наименования")
        s_qty += qty; s_bez += bez; s_nal += nal; s_uch += uch
    if dup: err(f"дублей КИЗ: {dup}")
    if bad_rate: err(f"строк, где сумма НДС не соответствует ставке: {bad_rate}")

    v = doc.find("ТаблСчФакт/ВсегоОпл")
    t_bez, t_uch, t_qty = D(v.get("СтТовБезНДСВсего")), D(v.get("СтТовУчНалВсего")), D(v.get("КолНеттоВс"))
    t_nal = D(v.findtext("СумНалВсего/СумНал"))
    if t_uch != s_uch: err(f"ВсегоОпл с НДС {t_uch} != сумма строк {s_uch}")
    if t_qty != s_qty: err(f"ВсегоОпл кол-во {t_qty} != сумма строк {s_qty}")
    if t_bez + t_nal != t_uch: err("ВсегоОпл: без НДС + НДС != с НДС")
    if (t_bez, t_nal) == (s_bez, s_nal):
        info("итоги: шапка == сумма строк (режим rows)")
    elif rate is not None and t_bez == q2(t_uch / (1 + rate)):
        warn(f"итоги: как в эталонах (round(итого/(1+ставка))), шапка != сумма строк, дрейф {t_bez - s_bez} руб. (режим reference)")
    else:
        err(f"итоги без НДС/НДС ({t_bez}/{t_nal}) не совпадают ни с суммой строк ({s_bez}/{s_nal}), ни с эталонной формулой")

    ttn = sf.find("ДокПодтвОтгрНом")
    sp = doc.find("СвПродПер/СвПер")
    if ttn is None or sp is None: err("нет ТТН или СвПер")
    else:
        info(f"УПД №{sf.get('НомерДок')} от {sf.get('ДатаДок')}; ТТН {ttn.get('РеквНомерДок')} от {ttn.get('РеквДатаДок')}; передача {sp.get('ДатаПер')}")
        if sp.find("ОснПер").get("РеквДатаДок") != sf.get("ДатаДок"):
            warn("дата договора != дате УПД (в обоих эталонах они равны)")
    _check_scheme(root, sf, sp, err, warn, info, expected_scheme)
    if root.get("ВремИнфПр") == "00.00.00" or doc.get("ВремИнфПр") == "00.00.00":
        warn("ВремИнфПр=00.00.00 (в эталонах — реальное время формирования)")
    return F


def _check_scheme(root, sf, sp, err, warn, info, expected=None):
    """Агентская схема (по умолчанию — с 01.10.2026): в документе не должно быть «комис» — Lamoda проверяет
    НаимОсн/НомОсн/Значен и ВидОпер; мы — строже, весь документ. Схему можно выбрать вручную (expected):
    тогда проверяется соответствие выбранной схеме, а расхождение с датой — предупреждение, не ошибка."""
    try:
        by_date = C.scheme_for(sf.get("ДатаДок"))
    except (AttributeError, ValueError):
        err(f"ДатаДок не в формате ДД.ММ.ГГГГ: {sf.get('ДатаДок')!r}")
        return
    scheme = expected or by_date
    manual = scheme.name != by_date.name
    label = {"commission": "комиссия", "agency": "агентский договор"}
    info(f"схема: {label[scheme.name]}" + (" (выбрана вручную)" if manual else ""))
    if manual:
        warn(f"по дате УПД {sf.get('ДатаДок')} положена схема «{label[by_date.name]}», "
             f"выбрана «{label[scheme.name]}» — убедитесь, что это намеренно")
    if sp is None or sp.find("ОснПер") is None:
        return
    if sp.get("ВидОпер") != scheme.vid_oper or sp.find("ОснПер").get("РеквНаимДок") != scheme.basis_name:
        err(f"ВидОпер/основание не соответствуют схеме «{label[scheme.name]}»: "
            f"{sp.get('ВидОпер')!r} / {sp.find('ОснПер').get('РеквНаимДок')!r}")
    if scheme.name == "agency":
        hits = [f"{el.tag}/@{k}={v!r}" for el in root.iter() for k, v in el.attrib.items()
                if "комис" in v.lower()]
        hits += [f"{el.tag}={el.text.strip()!r}" for el in root.iter()
                 if el.text and "комис" in el.text.lower()]
        for h in hits:
            err(f"агентская схема: «комис» недопустимо, Lamoda не сопоставит — {h}")
