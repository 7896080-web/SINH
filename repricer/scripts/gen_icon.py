"""Значок программы «Репрайсер» (`deploy/repricer.ico` и `priceapp/static/favicon.ico`).

Картинка описана арифметикой, а не положена двоичным файлом: цвет, поля и
формы — числа в этом файле, и `python scripts/gen_icon.py` собирает `.ico`
заново байт в байт (тест сверяет). Найденный где-то значок нельзя ни
перекрасить, ни объяснить; этот — можно.

Свой растеризатор, а не Pillow: тянуть библиотеку ради одной картинки значит
завести зависимость, которую чинить при каждом обновлении Python. Код — КОПИЯ
подхода `scripts/gen_icons.py` из sync_admin, а не импорт: программы не делят
код (`tests/test_isolation.py`).

Форма — ЦЕННИК (бирка с дырочкой), а не просто цвет: на той же машине бывают
ярлыки других программ, в панели задач значок 16 пикселей, и различать их надо
по силуэту. Цвет — `--accent` из `priceapp/static/style.css`: значок на столе и
шапка страницы, к которой он ведёт, для человека одна вещь.

Внутри `.ico` — BMP, а не PNG: BMP понимают все версии Windows, а отказ с PNG
был бы немым (значок просто не нарисовался бы).
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

ACCENT = (0x24, 0x57, 0xC5)      # --accent в style.css
WHITE = (0xFF, 0xFF, 0xFF)
SIZES = (16, 32, 48, 256)        # панель задач, стол, проводник, плитки
SS = 4                           # суперсэмплинг: сглаживание берётся только отсюда

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Ярлыку — все кадры; вкладке браузера хватит малых: кадр 256 весит 256 КБ,
# а страница отдаёт значок при каждом открытии.
OUTPUTS = ((os.path.join(HERE, "deploy", "repricer.ico"), SIZES),
           (os.path.join(HERE, "priceapp", "static", "favicon.ico"), (16, 32, 48)))


def _rounded_rect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _tri(x, y, p0, p1, p2):
    def side(a, b):
        return (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
    s0, s1, s2 = side(p0, p1), side(p1, p2), side(p2, p0)
    return (s0 >= 0 and s1 >= 0 and s2 >= 0) or (s0 <= 0 and s1 <= 0 and s2 <= 0)


def glyph_tag(x, y):
    """Ценник остриём влево. Дырочка прорезана ФОНОМ: белый кружок по белой
    бирке невидим, а вырез виден всегда. Её радиус 0.07 — на 16 пикселях это
    чуть больше пикселя; меньше — и дырочка исчезает, ценник становится стрелкой."""
    if (x - 0.33) ** 2 + (y - 0.50) ** 2 <= 0.07 ** 2:
        return False
    # Левый край бирки прямой (скругляем только правый): скруглённый угол у
    # стыка с остриём давал зазубрину, которую треугольник не перекрывает.
    body = (0.40 <= x <= 0.80 and 0.25 <= y <= 0.75) or _rounded_rect(x, y, 0.40, 0.25, 0.86, 0.75, 0.06)
    point = _tri(x, y, (0.40, 0.25), (0.40, 0.75), (0.14, 0.50))
    return body or point


def render(size: int) -> bytes:
    """Кадр size×size как BGRA снизу вверх — как его ждёт BMP внутри .ico."""
    n = size * SS
    m, radius = 0.04, 0.18
    samples = bytearray(n * n * 4)
    for sy in range(n):
        y = (sy + 0.5) / n
        for sx in range(n):
            x = (sx + 0.5) / n
            if not _rounded_rect(x, y, m, m, 1 - m, 1 - m, radius):
                continue
            c = WHITE if glyph_tag(x, y) else ACCENT
            i = (sy * n + sx) * 4
            samples[i:i + 4] = bytes((c[2], c[1], c[0], 255))
    out = bytearray(size * size * 4)
    for py in range(size):
        dst = (size - 1 - py) * size * 4          # BMP хранит строки снизу вверх
        for px in range(size):
            sa = sr = sg = sb = 0
            for dy in range(SS):
                base = ((py * SS + dy) * n + px * SS) * 4
                for dx in range(SS):
                    i = base + dx * 4
                    a = samples[i + 3]
                    if a:   # цвета, помноженные на альфу: иначе по краю тёмный ореол
                        sr += samples[i] * a
                        sg += samples[i + 1] * a
                        sb += samples[i + 2] * a
                        sa += a
            j = dst + px * 4
            if sa:
                out[j], out[j + 1], out[j + 2] = sr // sa, sg // sa, sb // sa
                out[j + 3] = sa // (SS * SS)
    return bytes(out)


def build_ico(frames: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries, blobs = [], []
    for size, pixels in frames:
        mask = b"\x00" * (((size + 31) // 32) * 4 * size)
        info = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
        blob = info + pixels + mask
        byte = 0 if size == 256 else size         # 256 пишется нулём: поле однобайтовое
        entries.append(struct.pack("<BBBBHHII", byte, byte, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    return header + b"".join(entries) + b"".join(blobs)


def icon_bytes(sizes=SIZES) -> bytes:
    return build_ico([(s, render(s)) for s in sizes])


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    for path, sizes in OUTPUTS:
        data = icon_bytes(sizes)
        with open(path, "wb") as f:
            f.write(data)
        print(f"{path}: {len(data)} bytes, frames {', '.join(map(str, sizes))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
