"""Сопоставление площадок между собой по ПУЛАМ баркодов (Фаза 1, ревизия).

Единица товара — SKU (карточка) с ПУЛОМ баркодов, а не отдельный баркод:
у одного товара несколько размеров, у каждого свой баркод. Правило
сопоставления (по требованию заказчика): два SKU — один и тот же товар,
если их пулы баркодов пересекаются хотя бы одним общим баркодом. Это же
верно и для SKU 1С (у номенклатуры 1С — свой пул баркодов).

Реализация — объединение (union-find) по баркодам: внутри одного SKU
площадки (account_id, external_id) все баркоды связываются в один пул,
внутри одного товара 1С (uid_1c) — тоже. Связные компоненты = размер-цвет
SKU, сопоставленные между площадками и с 1С. Ключ пула — размер-цвет
(WB nmID:chrtID, Kit id варианта, Ozon product_id): пулы маленькие (по данным
максимум ~4 баркода), гигантских слияний нет.

ВАЖНО: это слой сопоставления/визуализации. Разбор заказов остаётся
поштучным по баркоду (см. matching.resolve_barcode) — денежный путь не
меняется: каждый размер должен резолвиться своим баркодом.
"""
from collections import defaultdict

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.models import Barcode, Platform, PlatformAccount, PlatformCatalogItem, User
from app.excel_utils import build_xlsx_response

router = APIRouter()
templates = shared_templates

RESULT_LIMIT = 300

# По сколько значений за раз кладём в `IN (...)`. У SQLite есть предел на число
# параметров запроса (по умолчанию 999 в сборках до 3.32), и боевой каталог его
# превышает в шестнадцать раз.
_IN_CHUNK = 900


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        p = self.parent
        p.setdefault(x, x)
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:
            p[x], x = root, p[x]
        return root

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _barcodes_1c_near(db: Session, catalog_barcodes: set[str]) -> list[tuple[str, str]]:
    """Баркоды 1С, которые вообще могут попасть в кластер площадки.

    Читать ВСЮ таблицу баркодов было незачем и дорого: на боевом каталоге это
    154 232 строки на каждый запрос страницы, а сама страница перезапрашивает
    таблицу на каждой паузе в наборе (`hx-trigger="keyup"`). Замер на копии
    боевых данных: 1,33 с и 109 МБ пиковой памяти НА ЗАПРОС.

    При этом в кластер попадает только тот товар 1С, у которого хотя бы один
    баркод есть в каталоге площадок: кластеры заводятся по позициям каталога
    (цикл по `items` ниже), а баркоды 1С лишь довязывают к ним свой uid. Значит
    нужны баркоды ровно тех товаров 1С, что каталогом задеты, — на тех же
    данных 18 124 строки вместо 154 232, за 0,25 с. Результат побайтово тот же:
    отброшены строки, которые старый код прочитал бы и выбросил по `root in
    clusters`.
    """
    if not catalog_barcodes:
        return []
    known = list(catalog_barcodes)
    uids: set[str] = set()
    # Порциями: у SQLite предел на число параметров в IN, и он ниже, чем размер
    # боевого каталога.
    for start in range(0, len(known), _IN_CHUNK):
        uids.update(uid for (uid,) in db.query(Barcode.uid_1c).filter(
            Barcode.barcode.in_(known[start:start + _IN_CHUNK])).distinct().all())
    if not uids:
        return []
    rows: list[tuple[str, str]] = []
    uid_list = list(uids)
    for start in range(0, len(uid_list), _IN_CHUNK):
        rows.extend(db.query(Barcode.barcode, Barcode.uid_1c).filter(
            Barcode.uid_1c.in_(uid_list[start:start + _IN_CHUNK])).all())
    return rows


def _build_clusters(db: Session) -> list[dict]:
    """Строит кластеры товаров по пулам баркодов через все площадки и 1С."""
    items = db.query(
        PlatformCatalogItem.account_id, PlatformCatalogItem.external_id,
        PlatformCatalogItem.barcode, PlatformCatalogItem.article, PlatformCatalogItem.name,
    ).all()
    accounts = db.query(PlatformAccount).all()
    platmap = {a.id: a.platform.value for a in accounts}
    cabmap = {a.id: a.name for a in accounts}
    barcodes_1c = _barcodes_1c_near(db, {bc for _a, _e, bc, _ar, _n in items})
    uid_of = {bc: uid for bc, uid in barcodes_1c}

    uf = _UnionFind()
    # Пулы SKU площадок: (account_id, external_id) -> баркоды
    platform_pools: dict[tuple, list[str]] = defaultdict(list)
    for aid, ext, bc, _art, _name in items:
        uf.find(bc)
        platform_pools[(aid, ext)].append(bc)
    for bcs in platform_pools.values():
        for b in bcs[1:]:
            uf.union(bcs[0], b)
    # Пулы 1С: uid_1c -> баркоды
    pools_1c: dict[str, list[str]] = defaultdict(list)
    for bc, uid in barcodes_1c:
        uf.find(bc)
        pools_1c[uid].append(bc)
    for bcs in pools_1c.values():
        for b in bcs[1:]:
            uf.union(bcs[0], b)

    clusters: dict[str, dict] = {}

    def _c(root: str) -> dict:
        return clusters.setdefault(root, {
            "barcodes": set(), "platforms": set(), "cabinets": set(),
            "uid_1cs": set(), "articles": [], "names": [], "mapped_barcodes": set(),
        })

    for aid, ext, bc, art, name in items:
        c = _c(uf.find(bc))
        c["barcodes"].add(bc)
        c["platforms"].add(platmap[aid])
        c["cabinets"].add(f"{cabmap[aid]} ({platmap[aid].upper()})")
        if art:
            c["articles"].append(art)
        if name:
            c["names"].append(name)
        if bc in uid_of:
            c["mapped_barcodes"].add(bc)
            c["uid_1cs"].add(uid_of[bc])
    # Довязываем привязку к 1С через баркоды 1С, попавшие в кластер площадки
    for bc, uid in barcodes_1c:
        root = uf.find(bc)
        if root in clusters:
            clusters[root]["uid_1cs"].add(uid)
            clusters[root]["mapped_barcodes"].add(bc)

    result = []
    for c in clusters.values():
        platform_barcodes = c["barcodes"]
        mapped_in_platform = platform_barcodes & c["mapped_barcodes"]
        if not c["uid_1cs"]:
            status = "unmapped"
        elif mapped_in_platform >= platform_barcodes:
            status = "mapped"       # все баркоды площадок привязаны (заказы резолвятся)
        else:
            status = "partial"      # SKU сопоставлен (общий баркод есть), но не все размеры привязаны
        result.append({
            "rep_barcode": min(platform_barcodes),
            "barcodes": sorted(platform_barcodes),
            "barcode_count": len(platform_barcodes),
            "platforms": sorted(c["platforms"]),
            "platform_count": len(c["platforms"]),
            "cabinets": sorted(c["cabinets"]),
            "article": c["articles"][0] if c["articles"] else "",
            "name": c["names"][0] if c["names"] else "",
            "uid_1cs": sorted(c["uid_1cs"]),
            "status": status,
        })
    return result


def _filtered(db: Session, q: str, coverage: str, mapped: str) -> list[dict]:
    rows = _build_clusters(db)

    if coverage == "multi":
        rows = [r for r in rows if r["platform_count"] >= 2]
    if mapped == "mapped":
        rows = [r for r in rows if r["status"] in ("mapped", "partial")]
    elif mapped == "unmapped":
        rows = [r for r in rows if r["status"] == "unmapped"]
    if q:
        ql = q.lower()
        rows = [
            r for r in rows
            if ql in (r["article"] or "").lower()
            or ql in (r["name"] or "").lower()
            or any(ql in b.lower() for b in r["barcodes"])
            or any(ql in u.lower() for u in r["uid_1cs"])
        ]

    rows.sort(key=lambda r: (-r["platform_count"], -r["barcode_count"], r["rep_barcode"]))
    return rows


def _render(request: Request, db: Session, user: User, q: str, coverage: str, mapped: str, template: str):
    rows = _filtered(db, q, coverage, mapped)
    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "platform-matching",
        "rows": rows[:RESULT_LIMIT], "total": len(rows), "limit": RESULT_LIMIT,
        "q": q, "coverage": coverage, "mapped": mapped, "platforms": list(Platform),
    })


@router.get("/platform-matching", response_class=HTMLResponse)
def platform_matching_page(
    request: Request, q: str = Query(""), coverage: str = Query(""), mapped: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, q, coverage, mapped, "platform_matching.html")


@router.get("/platform-matching/rows", response_class=HTMLResponse)
def platform_matching_rows(
    request: Request, q: str = Query(""), coverage: str = Query(""), mapped: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """HTMX-фрагмент — только таблица, для живой фильтрации без перезагрузки."""
    return _render(request, db, user, q, coverage, mapped, "platform_matching_rows.html")


@router.get("/platform-matching/export")
def platform_matching_export(
    q: str = Query(""), coverage: str = Query(""), mapped: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Экспорт — ПО ОДНОЙ СТРОКЕ НА БАРКОД внутри отобранных кластеров, чтобы
    файл принимался тем же /mapping/import (привязка поштучно по баркоду —
    так резолвятся заказы каждого размера). Колонка «Товар (кластер)» помогает
    оператору проставить один ID_1С сразу на все баркоды одного товара."""
    clusters = _filtered(db, q, coverage, mapped)
    headers = ["ID_1С", "Баркод", "Товар (кластер)", "Артикул", "Площадки", "Статус"]
    data = []
    for c in clusters:
        uid = c["uid_1cs"][0] if len(c["uid_1cs"]) == 1 else None
        platforms = ", ".join(p.upper() for p in c["platforms"])
        for bc in c["barcodes"]:
            data.append([uid, bc, c["name"] or c["rep_barcode"], c["article"] or "", platforms, c["status"]])
    return build_xlsx_response(headers, data, "сопоставление_площадок.xlsx")
