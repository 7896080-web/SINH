"""Значок ярлыка «Финансы площадок» (`deploy/mpfinance.ico`).

Картинка описана арифметикой, а не положена готовым файлом: двоичный значок
нельзя ни перекрасить, ни объяснить, а `python scripts/gen_icon.py` собирает
его заново байт в байт. Растеризатор свой, без Pillow: его нет ни в
`requirements.txt`, ни на сервере, и заводить зависимость ради одной картинки
незачем. Устройство то же, что у значков Sync Admin (`scripts/gen_icons.py` в
корне репозитория), но код свой: программа ставится отдельным пакетом и чужих
файлов на сервере не видит.

**Отличается от соседей ФОРМОЙ, а не цветом.** На том же рабочем столе стоят
ярлыки Sync Admin (две встречные стрелки), «Возвраты» (коробка) и «Коробка
возвратов» (штрихкод). В панели задач значок — 16 пикселей, люди работают по
RDP, где цвет сжимается сильнее всего, — силуэты обязаны расходиться и
одноцветными. Здесь — круговая диаграмма с вынутым сектором: круг не похож ни
на стрелки, ни на прямоугольник коробки, ни на вертикальные полосы штрихкода.
Столбики «графика» были бы естественнее для финансов, но на 16 пикселях они —
тот же штрихкод.

BMP внутри `.ico`, а не PNG: PNG Windows понимает с Vista, но отказ был бы
немым (значок просто не нарисуется), а BMP понимают все.
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
from pathlib import Path

# --accent из finapp/static/style.css: значок и шапка страницы, к которой он
# ведёт, — одна вещь для человека.
ACCENT = (0x2F, 0x5D, 0x9F)
WHITE = (0xFF, 0xFF, 0xFF)
# 16 — панель задач, 32 — рабочий стол, 48 — проводник, 256 — плитки.
SIZES = (16, 32, 48, 256)
# Суперсэмплинг: фигуры с жёсткими краями, полутона — от усреднения блока.
SS = 4

OUT = Path(__file__).resolve().parent.parent / "deploy" / "mpfinance.ico"


def _rounded_rect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def glyph_pie(x, y):
    """Круг с вынутой четвертью; четверть сдвинута наружу.

    Радиус 0.30 от стороны — на 16 пикселях это круг около десяти пикселей,
    достаточно, чтобы вырез читался. Сдвиг сектора 0.07 — чуть больше пикселя:
    меньше — щель сливается, и остаётся просто круг; больше — сектор отрывается
    и читается как отдельная фигура. Вынута верхняя правая четверть (от 12 до 3
    часов): так диаграмма выглядит привычно.
    """
    cx = cy = 0.5
    r = 0.30
    shift = 0.07
    # Ось y в картинке идёт вниз; угол считаем как на циферблате, от «12» по часовой.
    def in_disc(px, py, ox=0.0, oy=0.0):
        dx, dy = px - (cx + ox), py - (cy + oy)
        return dx * dx + dy * dy <= r * r, math.atan2(dx, -dy)

    inside, angle = in_disc(x, y)
    if inside and not (0.0 <= angle <= math.pi / 2):
        return True
    # Вынутая четверть, сдвинутая вправо-вверх по своей биссектрисе.
    d = shift / math.sqrt(2)
    inside, angle = in_disc(x, y, d, -d)
    return inside and 0.0 <= angle <= math.pi / 2


def render(size: int) -> bytes:
    """Кадр size×size как BGRA снизу вверх — как его ждёт BMP."""
    n = size * SS
    m, radius = 0.04, 0.18
    samples = bytearray(n * n * 4)
    for sy in range(n):
        y = (sy + 0.5) / n
        for sx in range(n):
            x = (sx + 0.5) / n
            if not _rounded_rect(x, y, m, m, 1 - m, 1 - m, radius):
                continue
            color = WHITE if glyph_pie(x, y) else ACCENT
            i = (sy * n + sx) * 4
            samples[i:i + 4] = bytes((color[2], color[1], color[0], 255))
    out = bytearray(size * size * 4)
    for py in range(size):
        dst = (size - 1 - py) * size * 4          # BMP — строки снизу вверх
        for px in range(size):
            sa = sr = sg = sb = 0
            for dy in range(SS):
                base = ((py * SS + dy) * n + px * SS) * 4
                for dx in range(SS):
                    i = base + dx * 4
                    a = samples[i + 3]
                    if a:
                        # Цвет, помноженный на альфу: иначе по краю скругления
                        # прозрачные сэмплы дают тёмный ореол.
                        sr += samples[i] * a
                        sg += samples[i + 1] * a
                        sb += samples[i + 2] * a
                        sa += a
            j = dst + px * 4
            if sa:
                out[j:j + 4] = bytes((sr // sa, sg // sa, sb // sa, sa // (SS * SS)))
    return bytes(out)


def build_ico(frames: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries, blobs = [], []
    for size, pixels in frames:
        mask = b"\x00" * (((size + 31) // 32) * 4 * size)
        info = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                           len(pixels) + len(mask), 0, 0, 0, 0)
        blob = info + pixels + mask
        byte = 0 if size == 256 else size        # 256 в однобайтовое поле не влезает
        entries.append(struct.pack("<BBBBHHII", byte, byte, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    return header + b"".join(entries) + b"".join(blobs)


def icon_bytes(sizes=SIZES) -> bytes:
    return build_ico([(s, render(s)) for s in sizes])


def main() -> int:
    ap = argparse.ArgumentParser(description="Собрать deploy/mpfinance.ico")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    data = icon_bytes()
    Path(a.out).write_bytes(data)
    print(f"{a.out}: {len(data)} байт, кадры {', '.join(map(str, SIZES))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
