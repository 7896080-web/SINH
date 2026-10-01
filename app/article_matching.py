"""Сопоставление товаров площадки с 1С по АРТИКУЛАМ.

Основной путь — баркод (таблица «Баркоды»). Этот модуль закрывает остаток: товары
площадки, чьих баркодов в 1С нет. Правила не придумываются заранее, а ВЫВОДЯТСЯ
из данных (`analyze`): берутся пары, уже сопоставленные по баркоду, и для каждой
определяется, как артикул площадки соотносится с артикулом 1С. Что сработало на
этих парах — то и предлагается включить для кабинета.

Сравнение идёт по НОРМАЛИЗОВАННОМУ виду: верхний регистр, Ё→Е, кириллические
двойники латиницы (А/A, С/C, Х/X…) приводятся к латинице, все разделители и
пробелы выбрасываются. Поэтому «jn-100 / 48», «JN100_48» и «ЈN 100-48» с
кириллической «Н» дают один ключ, и отдельные правила «через дефис» или «через
подчёркивание» не нужны.

Виды соответствия (артикул площадки = …):
  exact       артикул 1С
  size        артикул 1С + размер
  color       артикул 1С + цвет
  size_color  артикул 1С + размер + цвет
  color_size  артикул 1С + цвет + размер
плюс постоянная приставка/окончание кабинета (strip_prefix/strip_suffix).

Предложение ОДНОЗНАЧНО, только если правила дают ровно один товар 1С (с учётом
размера площадки, если он известен — WB techSize). Связь создаётся только
подтверждением оператора (`confirm`) и только для баркода, которого ещё нет в
таблице «Баркоды»: существующую связь этот модуль не перепривязывает никогда.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import ArticleMatchRule, Barcode, MappingConflict, PlatformCatalogItem, Product

KINDS = ("exact", "size", "color", "size_color", "color_size")
KIND_LABELS = {
    "exact": "артикул совпадает",
    "size": "артикул + размер",
    "color": "артикул + цвет",
    "size_color": "артикул + размер + цвет",
    "color_size": "артикул + цвет + размер",
    "affix": "артикул 1С внутри, с приставкой/окончанием",
    "other": "не совпадает ни по одному правилу",
    "no_article": "нет артикула",
}
DEFAULT_KINDS = ("exact", "size")
SOURCE_TAG = "article_match"

# Кириллица, внешне неотличимая от латиницы, — частая причина «тот же артикул
# не находится»: в 1С набрано «А100», на площадке «A100».
_LOOKALIKES = str.maketrans("АВЕКМНОРСТУХІЈ", "ABEKMHOPCTYXIJ")
_NON_ALNUM = re.compile(r"[^0-9A-ZА-Я]+")


def normalize(text: str | None) -> str:
    if not text:
        return ""
    t = str(text).upper().replace("Ё", "Е").translate(_LOOKALIKES)
    return _NON_ALNUM.sub("", t)


def product_key(product: Product, kind: str) -> str:
    art, size, color = normalize(product.article), normalize(product.size), normalize(product.color)
    if not art:
        return ""
    if kind == "exact":
        return art
    if kind == "size":
        return art + size if size else ""
    if kind == "color":
        return art + color if color else ""
    if kind == "size_color":
        return art + size + color if size and color else ""
    if kind == "color_size":
        return art + color + size if size and color else ""
    return ""


def parse_kinds(raw: str | None) -> list[str]:
    return [k for k in (raw or "").split(",") if k in KINDS]


def get_rule(db: Session, account_id: int) -> ArticleMatchRule:
    rule = db.query(ArticleMatchRule).filter(ArticleMatchRule.account_id == account_id).first()
    if rule is None:
        rule = ArticleMatchRule(account_id=account_id, kinds=",".join(DEFAULT_KINDS))
        db.add(rule)
        db.flush()
    return rule


def strip_affixes(key: str, rule: ArticleMatchRule) -> str:
    prefix, suffix = normalize(rule.strip_prefix), normalize(rule.strip_suffix)
    if prefix and key.startswith(prefix):
        key = key[len(prefix):]
    if suffix and key.endswith(suffix):
        key = key[:-len(suffix)]
    return key


# --------------------------------------------------------------------- разбор

def _classify(platform_article: str, product: Product) -> tuple[str, str, str]:
    """(вид, приставка, окончание) для уже сопоставленной пары."""
    key = normalize(platform_article)
    if not key:
        return "no_article", "", ""
    for kind in KINDS:
        pk = product_key(product, kind)
        if pk and key == pk:
            return kind, "", ""
    # Артикул 1С внутри артикула площадки: остаток слева/справа — кандидат в
    # постоянную приставку/окончание кабинета. Размер/цвет в хвосте отрезаем,
    # чтобы «WB-JN100-48» дало приставку «WB», а не «WB» + «48».
    for kind in ("size_color", "color_size", "size", "color", "exact"):
        pk = product_key(product, kind)
        if pk and pk in key:
            i = key.index(pk)
            return "affix", key[:i], key[i + len(pk):]
    return "other", "", ""


@dataclass
class Analysis:
    total: int = 0
    counts: Counter = field(default_factory=Counter)
    examples: dict = field(default_factory=lambda: defaultdict(list))
    prefixes: Counter = field(default_factory=Counter)
    suffixes: Counter = field(default_factory=Counter)
    suggested_kinds: list = field(default_factory=list)
    suggested_prefix: str = ""
    suggested_suffix: str = ""
    unmatched: int = 0          # товаров кабинета без связи по баркоду


def analyze(db: Session, account_id: int, examples_per_kind: int = 3) -> Analysis:
    result = Analysis()
    mapped = dict(db.query(Barcode.barcode, Barcode.uid_1c).all())
    items = db.query(PlatformCatalogItem).filter(PlatformCatalogItem.account_id == account_id).all()
    products = {p.uid_1c: p for p in db.query(Product).filter(
        Product.uid_1c.in_({mapped[i.barcode] for i in items if i.barcode in mapped}))}

    seen_sku = set()
    for item in items:
        uid = mapped.get(item.barcode)
        if uid is None:
            result.unmatched += 1
            continue
        product = products.get(uid)
        # Одна пара на SKU площадки × товар 1С: альтернативные баркоды того же
        # размера не должны раздувать статистику.
        if product is None or (item.external_id, uid) in seen_sku:
            continue
        seen_sku.add((item.external_id, uid))
        kind, prefix, suffix = _classify(item.article, product)
        result.total += 1
        result.counts[kind] += 1
        if prefix:
            result.prefixes[prefix] += 1
        if suffix:
            result.suffixes[suffix] += 1
        if len(result.examples[kind]) < examples_per_kind:
            result.examples[kind].append({
                "platform": item.article or "", "article_1c": product.article or "",
                "size": product.size or "", "color": product.color or "",
                "platform_size": item.size or "",
            })

    # Предлагаем вид, если он объясняет хотя бы 5% пар (или хотя бы одну, когда
    # пар мало): редкие совпадения — скорее случайность, чем правило продавца.
    threshold = max(1, round(result.total * 0.05))
    result.suggested_kinds = [k for k in KINDS if result.counts[k] >= threshold]
    affix_total = result.counts["affix"]
    if affix_total >= threshold:
        for counter, attr in ((result.prefixes, "suggested_prefix"), (result.suffixes, "suggested_suffix")):
            if counter:
                value, n = counter.most_common(1)[0]
                if n * 2 >= affix_total:
                    setattr(result, attr, value)
        if result.suggested_prefix or result.suggested_suffix:
            # Приставка срезается, а под ней — один из обычных видов: какой именно,
            # определим по самим парам после среза.
            for item_kind in ("exact", "size"):
                if item_kind not in result.suggested_kinds:
                    result.suggested_kinds.append(item_kind)
    return result


# ----------------------------------------------------------------- кандидаты

@dataclass
class Candidate:
    item: PlatformCatalogItem
    status: str                       # unique / ambiguous / none / size_mismatch
    products: list = field(default_factory=list)
    kinds: list = field(default_factory=list)


def _index(products: list[Product], kinds: list[str]) -> dict[str, set]:
    index = defaultdict(set)
    for p in products:
        for kind in kinds:
            key = product_key(p, kind)
            if key:
                index[key].add((p.uid_1c, kind))
    return index


def candidates(db: Session, account_id: int, rule: ArticleMatchRule | None = None) -> list[Candidate]:
    """Подбор товара 1С для каждого НЕсопоставленного баркода кабинета."""
    rule = rule or get_rule(db, account_id)
    kinds = parse_kinds(rule.kinds)
    mapped = {b for (b,) in db.query(Barcode.barcode).all()}
    items = [i for i in db.query(PlatformCatalogItem).filter(
        PlatformCatalogItem.account_id == account_id).order_by(
        PlatformCatalogItem.article, PlatformCatalogItem.size, PlatformCatalogItem.barcode).all()
        if i.barcode and i.barcode not in mapped]
    if not items:
        return []
    products = db.query(Product).all()
    by_uid = {p.uid_1c: p for p in products}
    index = _index(products, kinds) if kinds else {}

    result = []
    for item in items:
        key = strip_affixes(normalize(item.article), rule)
        hits = index.get(key, set()) if key else set()
        uids = {uid for uid, _ in hits}
        status = "none"
        if uids and item.size:
            want = normalize(item.size)
            sized = {u for u in uids if normalize(by_uid[u].size) == want}
            if sized:
                uids = sized
            elif len(uids) > 1 or normalize(by_uid[next(iter(uids))].size):
                # Размер площадки известен и не совпал ни с одним размером 1С —
                # подставлять «похожий» размер нельзя: заказ списал бы чужой SKU.
                uids, status = set(), "size_mismatch"
        if uids:
            status = "unique" if len(uids) == 1 else "ambiguous"
        result.append(Candidate(
            item=item, status=status,
            products=sorted((by_uid[u] for u in uids), key=lambda p: (p.article or "", p.size or "")),
            kinds=sorted({k for u, k in hits if u in uids}),
        ))
    return result


def confirm(db: Session, account_id: int, pairs: list[tuple[str, str]]) -> tuple[int, dict]:
    """Создать связи баркод → товар 1С по подтверждённым предложениям.

    Предложения пересчитываются заново: подтверждается только то, что ПРЯМО
    СЕЙЧАС однозначно ведёт к указанному товару (между открытием страницы и
    нажатием могли смениться правила, каталог или справочник 1С). Существующую
    связь баркода не перепривязывает никогда. Возвращает (создано, {причина: n})."""
    current = {c.item.barcode: c for c in candidates(db, account_id)}
    created, refused = 0, Counter()
    for barcode, uid_1c in pairs:
        c = current.get(barcode)
        if c is None:
            refused["баркод уже сопоставлен или нет в каталоге кабинета"] += 1
        elif c.status != "unique" or c.products[0].uid_1c != uid_1c:
            refused["предложение изменилось — обновите страницу"] += 1
        else:
            db.add(Barcode(barcode=barcode, uid_1c=uid_1c, source_platform=SOURCE_TAG))
            db.query(MappingConflict).filter(MappingConflict.barcode == barcode).delete(
                synchronize_session=False)
            current.pop(barcode)          # дубль в той же пачке не создаст вторую строку
            created += 1
    db.flush()
    return created, dict(refused)
