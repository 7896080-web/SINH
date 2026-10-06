"""Этикетки 58×40 с кодом маркировки — как в «Маркировке» (ТЗ, 7.3).

GS1 DataMatrix ECC200, квадратный, libzint в режиме GS1. **Каждый символ до
выдачи читается обратно декодером** (zxing-cpp) и сверяется побайтно; не
совпало — PDF не выдаётся. Главная строка — название из карточки Нацкаталога
(артикула поставщика у кодов вне Lamoda нет), строки — шаблоны с подстановками.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from sqlalchemy.orm import Session

from kizapp.models import LabelTemplate, Setting
from kizapp.service import FULL_RE

FONTS = Path(__file__).resolve().parent / "fonts"
LABEL_W_MM, LABEL_H_MM = 58.0, 40.0
MIN_FONT, TITLE_FONT, TEXT_FONT = 5.0, 8.5, 7.0
PLACEHOLDERS = ("название", "GTIN", "изготовитель", "ИНН", "дата", "номер", "всего", "партия")
DEFAULTS = {
    "label_title": "{название}",
    "label_right": "GTIN {GTIN}",
    "label_bottom": "Изготовитель: {изготовитель}, ИНН {ИНН}\n{дата}  №{номер}/{всего}",
    "label_module": "0.5",                 # 203 dpi: ровно 4 точки на модуль; 300 dpi — 0.508
}
_PH = re.compile(r"\{([^{}]*)\}")


class LabelError(ValueError):
    pass


def get(db: Session, key: str) -> str:
    s = db.get(Setting, key)
    return s.value if s is not None else DEFAULTS[key]


def put(db: Session, key: str, value: str) -> None:
    s = db.get(Setting, key) or Setting(key=key)
    s.value = value
    db.add(s)


# --- Шаблоны: несколько, один основной ------------------------------------------------

def templates(db: Session) -> list[LabelTemplate]:
    """Все шаблоны; пусто — создаётся «Основной» из прежней настройки (или умолчаний)."""
    rows = db.query(LabelTemplate).order_by(LabelTemplate.is_default.desc(), LabelTemplate.name).all()
    if not rows:
        t = LabelTemplate(name="Основной", title=get(db, "label_title"), right=get(db, "label_right"),
                          bottom=get(db, "label_bottom"), module=get(db, "label_module"), is_default=1)
        db.add(t)
        db.flush()
        rows = [t]
    return rows


def template(db: Session, template_id: int | None) -> LabelTemplate:
    rows = templates(db)
    if template_id:
        t = db.get(LabelTemplate, template_id)
        if t is None:
            raise LabelError("шаблон не найден")
        return t
    return next((t for t in rows if t.is_default), rows[0])


def save_template(db: Session, template_id: int | None, name: str, title: str, right: str, bottom: str,
                  module: str) -> LabelTemplate:
    name = name.strip()
    problems = [] if name else ["нужно имя шаблона"]
    problems += check_template(title + right + bottom)
    try:
        m = float(module.replace(",", "."))
        if not 0.3 <= m <= 1.0:
            raise ValueError
    except ValueError:
        problems.append("модуль — от 0,3 до 1 мм (203 dpi: 0,5; 300 dpi: 0,508)")
        m = 0.5
    clash = db.query(LabelTemplate).filter(LabelTemplate.name == name, LabelTemplate.id != (template_id or 0)).first()
    if clash:
        problems.append(f"шаблон «{name}» уже есть")
    if problems:
        raise LabelError("; ".join(problems))
    t = db.get(LabelTemplate, template_id) if template_id else None
    if t is None:
        t = LabelTemplate(is_default=0 if templates(db) else 1)
        db.add(t)
    t.name, t.title, t.right, t.bottom, t.module = name, title.strip(), right.strip(), bottom.strip(), str(m)
    db.flush()
    return t


def make_default(db: Session, template_id: int) -> None:
    for t in templates(db):
        t.is_default = 1 if t.id == template_id else 0


def delete_template(db: Session, template_id: int) -> None:
    rows = templates(db)
    t = db.get(LabelTemplate, template_id)
    if t is None:
        return
    if len(rows) == 1:
        raise LabelError("последний шаблон удалить нельзя")
    was_default = t.is_default
    db.delete(t)
    db.flush()
    if was_default:
        templates(db)[0].is_default = 1


def check_template(text: str) -> list[str]:
    return [f"неизвестная подстановка {{{n}}}" for n in _PH.findall(text) if n not in PLACEHOLDERS]


def render_lines(template: str, values: dict) -> list[str]:
    """Строка, где все подстановки пусты, не печатается."""
    out = []
    for line in template.splitlines():
        names = _PH.findall(line)
        if names and all(not str(values.get(n, "")).strip() for n in names):
            continue
        text = _PH.sub(lambda m: str(values.get(m.group(1), "")), line).strip()
        if text:
            out.append(text)
    return out


def matrix(code: str) -> list[list[int]]:
    import zint
    m = FULL_RE.match(code)
    if m is None:
        raise LabelError("не полный код маркировки")
    sym = zint.Symbol()
    sym.symbology = zint.Symbology.DATAMATRIX
    sym.input_mode = zint.InputMode.GS1
    sym.option_3 = zint.DataMatrixOptions.SQUARE
    sym.encode(f"[01]{m.group(1)}[21]{m.group(2)}[91]{m.group(3)}[92]{m.group(4)}")
    packed = sym.encoded_data.tolist()
    return [[(packed[r][c >> 3] >> (c & 7)) & 1 for c in range(sym.width)] for r in range(sym.rows)]


def verify(code: str, m: list[list[int]]) -> None:
    """Рисуем символ с полем в 2 модуля и читаем обратно. Сверка побайтная."""
    import zxingcpp
    from PIL import Image
    px, quiet = 4, 2
    size = (len(m) + 2 * quiet) * px
    img = Image.new("L", (size, size), 255)
    pix = img.load()
    for r, row in enumerate(m):
        for c, bit in enumerate(row):
            if bit:
                for dy in range(px):
                    for dx in range(px):
                        pix[(c + quiet) * px + dx, (r + quiet) * px + dy] = 0
    found = zxingcpp.read_barcodes(img, text_mode=zxingcpp.TextMode.Plain)
    if not found or found[0].symbology_identifier != "]d2" or found[0].text != code:
        raise LabelError("этикетка не прошла проверку декодером — PDF не выдан")


def values_for(db: Session, codes: list[str], org, date_text: str, batch_id: int) -> list[dict]:
    """Значения подстановок; название — из справочника НК ИП партии."""
    from kizapp.service import card as card_of
    out = []
    org_name, inn = org.name, org.inn
    for i, code in enumerate(codes, 1):
        g = FULL_RE.match(code).group(1)
        card = card_of(db, org.id, g)
        out.append({"название": (card.name if card is not None and card.name else f"GTIN {g}"), "GTIN": g,
                    "изготовитель": org_name, "ИНН": inn, "дата": date_text, "номер": str(i),
                    "всего": str(len(codes)), "партия": str(batch_id)})
    return out


def _fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if "KzDV" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("KzDV", str(FONTS / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("KzDVB", str(FONTS / "DejaVuSans-Bold.ttf")))


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
    s = size
    while s >= MIN_FONT:
        wrapped = [w for ln in lines for w in _wrap(c, ln, font, s, width)]
        if not any(c.stringWidth(w, font, s) > width for w in wrapped) and len(wrapped) * s * leading <= height:
            return wrapped, s, True
        s -= 0.5
    return [w for ln in lines for w in _wrap(c, ln, font, MIN_FONT, width)], MIN_FONT, False


def build_pdf(db: Session, codes: list[str], values: list[dict],
              tpl: LabelTemplate | None = None) -> tuple[bytes, list[str]]:
    """PDF, страница 58×40 мм на этикетку. Возвращает (pdf, предупреждения)."""
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    _fonts()
    tpl = tpl or template(db, None)
    module = float(tpl.module)
    t_title, t_right, t_bottom = tpl.title, tpl.right, tpl.bottom
    buf = io.BytesIO()
    W, H = LABEL_W_MM * mm, LABEL_H_MM * mm
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle("Этикетки")
    warnings = []
    for code, v in zip(codes, values):
        m = matrix(code)
        verify(code, m)
        n = len(m)
        margin = 2.5 * mm
        side = n * module * mm
        x0, ytop = margin + module * mm, H - margin - module * mm
        c.setFillGray(0)
        for r, row in enumerate(m):
            k = 0
            while k < n:
                if row[k]:
                    s = k
                    while k < n and row[k]:
                        k += 1
                    c.rect(x0 + s * module * mm, ytop - (r + 1) * module * mm, (k - s) * module * mm,
                           module * mm, stroke=0, fill=1)
                else:
                    k += 1
        tx = x0 + side + 2 * mm
        right_w = W - tx - margin
        tl, ts, ok1 = _fit(c, render_lines(t_title, v), "KzDVB", TITLE_FONT, right_w, side * 0.72)
        y = ytop - ts
        c.setFont("KzDVB", ts)
        for ln in tl:
            c.drawString(tx, y, ln)
            y -= ts * 1.25
        rl, rs, ok2 = _fit(c, render_lines(t_right, v), "KzDV", TEXT_FONT, right_w,
                           max(y - (ytop - side) + TEXT_FONT, 0))
        c.setFont("KzDV", rs)
        for ln in rl:
            c.drawString(tx, y, ln)
            y -= rs * 1.25
        by = ytop - side - 1.5 * mm
        bl, bs, ok3 = _fit(c, render_lines(t_bottom, v), "KzDV", TEXT_FONT, W - 2 * margin, by - margin)
        c.setFont("KzDV", bs)
        y = by - bs
        for ln in bl:
            c.drawString(margin, y, ln)
            y -= bs * 1.25
        if not (ok1 and ok2 and ok3):
            warnings.append(f"№{v['номер']}: текст не влез даже мелким шрифтом")
        if v["название"].startswith("GTIN "):
            warnings.append(f"№{v['номер']}: нет карточки НК — на этикетке GTIN вместо названия")
        c.showPage()
    c.save()
    return buf.getvalue(), warnings
