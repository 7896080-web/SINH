"""Сопоставление товаров площадки с 1С по АРТИКУЛАМ.

Основной путь — баркод (справочник баркодов 1С, `mapping.py`). Этот модуль закрывает остаток: товары
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
подтверждением оператора (`confirm`, запись `ManualLink`) и только для баркода,
которого нет в справочнике 1С и который не сопоставлен иначе: существующую связь
этот модуль не перепривязывает никогда.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from priceapp import mapping
from priceapp.models import ArticleMatchRule, ManualLink, OnecBarcode, PlatformItem

@dataclass
class Sku:
    """Размер-цвет SKU 1С — из справочника баркодов (одна запись на item_id)."""
    uid_1c: str
    article: str
    name: str
    size: str
    color: str


def skus(db: Session) -> list[Sku]:
    seen: dict[str, Sku] = {}
    for b in db.query(OnecBarcode).order_by(OnecBarcode.id):
        if b.item_id not in seen:
            seen[b.item_id] = Sku(b.item_id, b.article, b.name, b.size, b.color)
    return list(seen.values())


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


def product_key(product: "Sku", kind: str) -> str:
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

def _classify(platform_article: str, product: "Sku") -> tuple[str, str, str]:
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
    by_uid = {k.uid_1c: k for k in skus(db)}
    seen_sku = set()
    for row in mapping.build(db, account_id):
        if row.status not in ("ok", "pool"):
            if row.status == "not_in_1c":
                result.unmatched += 1
            continue
        product = by_uid.get(row.item_id)
        item = row.item
        # Одна пара на размер площадки × SKU 1С: альтернативные баркоды того же
        # размера не должны раздувать статистику.
        if product is None or (item.external_id, row.item_id) in seen_sku:
            continue
        seen_sku.add((item.external_id, row.item_id))
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
    item: PlatformItem
    status: str                       # unique / ambiguous / none / size_mismatch
    products: list = field(default_factory=list)
    kinds: list = field(default_factory=list)


def _index(products: list["Sku"], kinds: list[str]) -> dict[str, set]:
    index = defaultdict(set)
    for p in products:
        for kind in kinds:
            key = product_key(p, kind)
            if key:
                index[key].add((p.uid_1c, kind))
    return index


def candidates(db: Session, account_id: int, rule: ArticleMatchRule | None = None) -> list[Candidate]:
    """Подбор SKU 1С для каждого баркода кабинета, который «нет в 1С»."""
    rule = rule or get_rule(db, account_id)
    kinds = parse_kinds(rule.kinds)
    items = [r.item for r in mapping.build(db, account_id) if r.status == "not_in_1c"]
    if not items:
        return []
    products = skus(db)
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
                # Размер площадки известен и не совпал — «похожий» не подставляем:
                # цена посчиталась бы от себестоимости чужого SKU.
                uids, status = set(), "size_mismatch"
        if uids:
            status = "unique" if len(uids) == 1 else "ambiguous"
        result.append(Candidate(
            item=item, status=status,
            products=sorted((by_uid[u] for u in uids), key=lambda p: (p.article or "", p.size or "")),
            kinds=sorted({k for u, k in hits if u in uids}),
        ))
    return result


def confirm(db: Session, account_id: int, pairs: list[tuple[str, str]], actor: str = "") -> tuple[int, dict]:
    """Создать связи баркод -> SKU 1С по подтверждённым предложениям.

    Предложения пересчитываются заново: подтверждается только то, что ПРЯМО
    СЕЙЧАС однозначно ведёт к указанному SKU. Возвращает (создано, {причина: n})."""
    current = {c.item.barcode: c for c in candidates(db, account_id)}
    created, refused = 0, Counter()
    for barcode, uid in pairs:
        c = current.get(barcode)
        if c is None:
            refused["баркод уже сопоставлен или нет в каталоге кабинета"] += 1
        elif c.status != "unique" or c.products[0].uid_1c != uid:
            refused["предложение изменилось — обновите страницу"] += 1
        else:
            db.add(ManualLink(account_id=account_id, barcode=barcode, item_id=uid,
                              source=SOURCE_TAG, created_by=actor))
            from priceapp.database import session_cache
            session_cache(db).clear()   # сборка в этой же транзакции должна увидеть связь
            current.pop(barcode)
            created += 1
    db.flush()
    return created, dict(refused)
