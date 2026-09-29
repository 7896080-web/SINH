"""Этикетки 58×40 с кодом маркировки (ТЗ, 7.3).

**Главная строка — размерный артикул поставщика** (`3030 KAHVE 4XL АВЕР
Рубашка Д/р сатин`): цвет и размер в нём уже есть (решение заказчика), поэтому
этикетке не нужен Нацкаталог. Артикул находится по GTIN, вынутому из самого
кода, через справочник GTIN. Нет пары — печатается название карточки НК, если
она есть, иначе GTIN, и человек видит предупреждение ДО печати.

Кодирование — GS1 DataMatrix ECC200, квадратный, libzint в режиме GS1
(`[01]…[21]…[91]…[92]…`). **Каждый символ до выдачи читается обратно
декодером** (`zxing-cpp`) и сверяется с исходным кодом побайтно; не совпало —
этикетка не выдаётся. На всех 338 кодах поставки 12550 это проверено, и три
этикетки прочитало приложение «Честный знак».
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from markapp import settings
from markapp.models import GtinPair, NkCard, Organization

GS = "\x1d"
FONTS = Path(__file__).resolve().parent / "fonts"
# Полный код одежды: 01 + GTIN(14) + 21 + серийный(13) + GS 91 ключ(4) + GS 92 подпись(44).
FULL_RE = re.compile(r"^01(\d{14})21(.{13})\x1d91(.{4})\x1d92(.{44})$", re.S)
MAX_LABELS = 5000

LABEL_W_MM, LABEL_H_MM = 58.0, 40.0
DEFAULT_MODULE_MM = 0.5          # 203 dpi: ровно 4 точки на модуль
MIN_FONT, TITLE_FONT, TEXT_FONT = 5.0, 8.5, 7.0

LABEL_TITLE = settings.LABEL_TITLE
LABEL_RIGHT = settings.LABEL_RIGHT
LABEL_BOTTOM = settings.LABEL_BOTTOM
LABEL_MODULE = settings.LABEL_MODULE
PLACEHOLDERS = ("артикул", "цвет", "размер", "название", "GTIN", "EAN", "изготовитель", "ИНН",
                "дата", "номер", "всего", "поставка")
_PH = re.compile(r"\{([^{}]*)\}")


class LabelError(ValueError):
    pass


# --- Коды ---------------------------------------------------------------------

def normalize(raw: str) -> str:
    """GS бывает в любой записи: настоящий 0x1D, `_x001D_` из xlsx, `\\u001d`."""
    return (raw.strip().replace("_x001D_", GS).replace("_x001d_", GS)
            .replace("\\u001d", GS).replace("\\u001D", GS))


def parse_codes(text: str) -> tuple[list[str], list[str]]:
    """(коды, ошибки). Строки делятся ТОЛЬКО по переводу строки: `splitlines()`
    считает GS концом строки и режет каждый код на три куска."""
    codes, errors, seen = [], [], set()
    for n, raw in enumerate(re.split(r"\r\n|\n|\r", text), start=1):
        if not raw.strip():
            continue
        code = normalize(raw)
        if not FULL_RE.match(code):
            short = len(code) == 31 and code.startswith("01")
            errors.append(f"строка {n}: " + ("короткий код без криптохвоста — для этикетки нужен полный"
                                             if short else "не похоже на полный код маркировки"))
            continue
        if code in seen:
            errors.append(f"строка {n}: код повторяется в файле")
            continue
        seen.add(code)
        codes.append(code)
    if len(codes) > MAX_LABELS:
        errors.append(f"кодов больше {MAX_LABELS} — разбейте на несколько файлов")
    return codes, errors


def gs1_input(code: str) -> str:
    m = FULL_RE.match(code)
    if m is None:
        raise LabelError("не полный код маркировки")
    return f"[01]{m.group(1)}[21]{m.group(2)}[91]{m.group(3)}[92]{m.group(4)}"


def matrix(code: str) -> list[list[int]]:
    import zint
    sym = zint.Symbol()
    sym.symbology = zint.Symbology.DATAMATRIX
    sym.input_mode = zint.InputMode.GS1
    sym.option_3 = zint.DataMatrixOptions.SQUARE
    sym.encode(gs1_input(code))
    packed = sym.encoded_data.tolist()
    return [[(packed[r][c >> 3] >> (c & 7)) & 1 for c in range(sym.width)] for r in range(sym.rows)]


def verify(code: str, m: list[list[int]]) -> None:
    """Рисуем символ с полем в 2 модуля и читаем обратно. Сверка побайтная."""
    import zxingcpp
    from PIL import Image
    px, quiet = 4, 2
    n = len(m)
    size = (n + 2 * quiet) * px
    img = Image.new("L", (size, size), 255)
    pix = img.load()
    for r, row in enumerate(m):
        for c, bit in enumerate(row):
            if bit:
                for dy in range(px):
                    for dx in range(px):
                        pix[(c + quiet) * px + dx, (r + quiet) * px + dy] = 0
    found = zxingcpp.read_barcodes(img, text_mode=zxingcpp.TextMode.Plain)
    if not found:
        raise LabelError("декодер не прочитал символ")
    if found[0].symbology_identifier != "]d2":
        raise LabelError(f"символ не GS1 DataMatrix ({found[0].symbology_identifier})")
    if found[0].text != code:
        raise LabelError("прочитанный код не совпал с исходным")


# --- Содержимое ---------------------------------------------------------------

@dataclass
class LabelData:
    code: str
    values: dict
    warnings: list = field(default_factory=list)


def label_values(db: Session, codes: list[str], org: Organization | None,
                 date_text: str, supply_number: str = "") -> list[LabelData]:
    gtins = {FULL_RE.match(c).group(1) for c in codes}
    pairs = {p.gtin: p for p in db.query(GtinPair).filter(GtinPair.gtin.in_(gtins)).all()}
    cards = {c.gtin: c for c in db.query(NkCard).filter(NkCard.gtin.in_(gtins)).all()}
    from markapp.models import CatalogItem
    skus = [p.supplier_sku for p in pairs.values()]
    catalog = {c.supplier_sku: c for c in db.query(CatalogItem).filter(CatalogItem.supplier_sku.in_(skus)).all()}
    out = []
    for i, code in enumerate(codes, 1):
        g = FULL_RE.match(code).group(1)
        pair, card = pairs.get(g), cards.get(g)
        item = catalog.get(pair.supplier_sku) if pair else None
        nk_ok = card is not None and card.status == "ok"
        warnings = []
        if pair is None:
            warnings.append(f"GTIN {g} не сопоставлен с артикулом — на этикетке будет "
                            + ("название из Нацкаталога" if nk_ok else "только GTIN"))
        values = {
            "артикул": pair.supplier_sku if pair else (card.name if nk_ok else f"GTIN {g}"),
            "цвет": (item.color if item and item.color else (card.color if nk_ok else "")),
            "размер": (item.size if item and item.size else (card.size if nk_ok else "")),
            "название": (card.name if nk_ok else (item.title if item else "")),
            "GTIN": g,
            "EAN": item.ean if item else "",
            "изготовитель": org.name if org else "",
            "ИНН": org.inn if org else "",
            "дата": date_text,
            "номер": str(i),
            "всего": str(len(codes)),
            "поставка": supply_number,
        }
        out.append(LabelData(code, values, warnings))
    return out


def check_template(text: str) -> list[str]:
    """Неизвестная подстановка — ошибка при сохранении, а не `{…}` на этикетке."""
    return [f"неизвестная подстановка {{{name}}}" for name in _PH.findall(text)
            if name not in PLACEHOLDERS]


def fill(template: str, values: dict) -> str:
    return _PH.sub(lambda m: str(values.get(m.group(1), "")), template)


def render_lines(template: str, values: dict) -> list[str]:
    """Строка, в которой все подстановки пусты, не печатается."""
    out = []
    for line in template.splitlines():
        names = _PH.findall(line)
        if names and all(not str(values.get(n, "")).strip() for n in names):
            continue
        text = fill(line, values).strip()
        if text:
            out.append(text)
    return out


# --- PDF ----------------------------------------------------------------------

def _fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if "MkDV" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("MkDV", str(FONTS / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("MkDVB", str(FONTS / "DejaVuSans-Bold.ttf")))


def _wrap(c, text, font, size, width):
    out, cur = [], ""
    for word in text.split():
        t = (cur + " " + word).strip()
        if c.stringWidth(t, font, size) <= width or not cur:
            cur = t
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out


def _fit(c, lines, font, size, width, height, leading=1.25):
    """Уменьшаем шрифт, пока текст не влезет; не влез и на минимуме — False."""
    s = size
    while s >= MIN_FONT:
        wrapped = [w for ln in lines for w in _wrap(c, ln, font, s, width)]
        too_wide = any(c.stringWidth(w, font, s) > width for w in wrapped)
        if not too_wide and len(wrapped) * s * leading <= height:
            return wrapped, s, True
        s -= 0.5
    wrapped = [w for ln in lines for w in _wrap(c, ln, font, MIN_FONT, width)]
    return wrapped, MIN_FONT, False


def build_pdf(db: Session, data: list[LabelData]) -> tuple[bytes, list[str]]:
    """PDF, одна страница 58×40 мм на этикетку. Возвращает (pdf, предупреждения)."""
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    _fonts()
    module = float(settings.get(db, LABEL_MODULE) or DEFAULT_MODULE_MM)
    t_title = settings.get(db, LABEL_TITLE)
    t_right = settings.get(db, LABEL_RIGHT)
    t_bottom = settings.get(db, LABEL_BOTTOM)
    buf = io.BytesIO()
    W, H = LABEL_W_MM * mm, LABEL_H_MM * mm
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle("Этикетки")
    warnings: list[str] = []
    for d in data:
        m = matrix(d.code)
        verify(d.code, m)                      # не прочиталось — исключение, PDF не выдаётся
        n = len(m)
        margin = 2.5 * mm
        side = n * module * mm
        x0, ytop = margin + module * mm, H - margin - module * mm   # поле в 1 модуль
        c.setFillGray(0)
        for r, row in enumerate(m):
            k = 0
            while k < n:
                if row[k]:
                    s = k
                    while k < n and row[k]:
                        k += 1
                    c.rect(x0 + s * module * mm, ytop - (r + 1) * module * mm,
                           (k - s) * module * mm, module * mm, stroke=0, fill=1)
                else:
                    k += 1
        # Справа от кода: заголовок (артикул) и строки.
        tx = x0 + side + 2 * mm
        right_w = W - tx - margin
        title = render_lines(t_title, d.values)
        right = render_lines(t_right, d.values)
        tl, ts, ok1 = _fit(c, title, "MkDVB", TITLE_FONT, right_w, side * 0.72)
        y = ytop - ts
        c.setFont("MkDVB", ts)
        for ln in tl:
            c.drawString(tx, y, ln)
            y -= ts * 1.25
        rl, rs, ok2 = _fit(c, right, "MkDV", TEXT_FONT, right_w, max(y - (ytop - side) + TEXT_FONT, 0))
        c.setFont("MkDV", rs)
        for ln in rl:
            c.drawString(tx, y, ln)
            y -= rs * 1.25
        # Под кодом: строки на всю ширину.
        bottom = render_lines(t_bottom, d.values)
        by = ytop - side - 1.5 * mm
        bl, bs, ok3 = _fit(c, bottom, "MkDV", TEXT_FONT, W - 2 * margin, by - margin)
        c.setFont("MkDV", bs)
        y = by - bs
        for ln in bl:
            c.drawString(margin, y, ln)
            y -= bs * 1.25
        if not (ok1 and ok2 and ok3):
            warnings.append(f"№{d.values['номер']} ({d.values['артикул']}): текст не влез даже мелким шрифтом")
        warnings.extend(f"№{d.values['номер']}: {w}" for w in d.warnings)
        c.showPage()
    c.save()
    return buf.getvalue(), warnings

