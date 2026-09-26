"""Генератор значков для ярлыков на рабочем столе (`deploy/*.ico`).

Зачем генератор, а не просто положенный в репозиторий файл.

Значок — двоичный файл, и «откуда он взялся» по нему не прочитать. Найденный
в интернете значок нельзя ни перекрасить, ни поправить, ни объяснить; через
полгода единственным способом изменить его будет искать похожий заново.
Здесь картинка описана арифметикой: цвет, поля, толщины — числа в этом файле,
и `python3 scripts/gen_icons.py` собирает `.ico` заново, байт в байт.

Почему свой растеризатор, а не Pillow. Его нет ни в `requirements.txt`, ни на
боевом сервере, а тянуть библиотеку ради двух картинок, которые рисуются
двумя десятками строк, значит завести зависимость, которую потом придётся
чинить при каждом обновлении Python.

**Значки обязаны различаться ФОРМОЙ, а не только цветом.** Ярлыки стоят рядом,
в панели задач они 16 пикселей, а на сервере люди работают по RDP, где цвет
сжимается сильнее всего. Поэтому у админки — две встречные стрелки (обмен), у
возвратов — коробка: силуэты не спутать даже одноцветными.

Почему BMP внутри `.ico`, а не PNG. PNG внутри `.ico` Windows понимает с Vista,
и на боевом Windows Server 2016 он работал бы. Но отказ был бы немым — значок
просто не нарисовался бы, а разбираться пришлось бы на сервере без отладки;
BMP понимают все версии, а лишние килобайты патч всё равно жмёт zlib.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

# Цвета из палитры приложения (`app/static/style.css`): --accent и --good.
# Совпадение не косметика: значок на столе и шапка страницы, к которой он
# ведёт, — одна вещь для человека, и разный синий читается как разные системы.
ACCENT = (0x2F, 0x5D, 0x9F)
GOOD = (0x2F, 0x7D, 0x4F)
WHITE = (0xFF, 0xFF, 0xFF)

# Размеры кадров в одном файле. 16 — панель задач, 32 — рабочий стол, 48 —
# крупные значки проводника, 256 — плитки и «очень крупные». Промежуточные
# Windows получает уменьшением ближайшего большего, и это заметно хуже только
# на 64–128, где значок почти не показывают.
SIZES = (16, 32, 48, 256)

# Кратность суперсэмплинга. Сглаживание тут берётся только отсюда: фигуры
# рисуются с жёсткими краями, а усреднение блока SS×SS и даёт полутона.
SS = 4


def _rounded_rect(x, y, x0, y0, x1, y1, r):
    """Точка внутри прямоугольника со скруглёнными углами."""
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _rect(x, y, x0, y0, x1, y1):
    return x0 <= x <= x1 and y0 <= y <= y1


def _tri(x, y, p0, p1, p2):
    """Точка внутри треугольника — по знакам трёх векторных произведений."""
    def side(a, b):
        return (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
    s0, s1, s2 = side(p0, p1), side(p1, p2), side(p2, p0)
    return (s0 >= 0 and s1 >= 0 and s2 >= 0) or (s0 <= 0 and s1 <= 0 and s2 <= 0)


def glyph_sync(x, y):
    """Две встречные стрелки — обмен остатками с площадками.

    Толщины подобраны под 16 пикселей, где значок и живёт в панели задач.
    Древко 0.13 от стороны — это ровно два пикселя; при 0.10 выходило полтора,
    и хвост стрелки превращался в серую рябь, от которой оставался один
    наконечник. Зазор между стрелками 0.06 — примерно пиксель, меньше значит
    «одна толстая полоса».
    """
    # верхняя, вправо
    if _rect(x, y, 0.16, 0.275, 0.60, 0.405):
        return True
    if _tri(x, y, (0.58, 0.21), (0.58, 0.47), (0.84, 0.34)):
        return True
    # нижняя, влево
    if _rect(x, y, 0.40, 0.595, 0.84, 0.725):
        return True
    if _tri(x, y, (0.42, 0.53), (0.42, 0.79), (0.16, 0.66)):
        return True
    return False


def glyph_box(x, y):
    """Коробка с заклеенным стыком — вещь, приехавшая из ПВЗ.

    Щели прорезаны ФОНОМ, а не нарисованы линией: белая линия по белому телу
    коробки невидима, а вырез виден всегда, каким бы ни был фон.
    """
    if not _rect(x, y, 0.14, 0.26, 0.86, 0.80):
        return False
    if _rect(x, y, 0.14, 0.44, 0.86, 0.50):   # стык крышки и корпуса
        return False
    if _rect(x, y, 0.47, 0.26, 0.53, 0.44):   # стык клапанов
        return False
    return True


ICONS = {
    "sync_admin": (ACCENT, glyph_sync),
    "returns": (GOOD, glyph_box),
}


def render(size: int, bg, glyph) -> bytes:
    """Кадр size×size как BGRA снизу вверх — в том виде, в каком его ждёт BMP."""
    n = size * SS
    br, bgc, bb = bg[2], bg[1], bg[0]
    wr, wg, wb = WHITE[2], WHITE[1], WHITE[0]
    # Поле по краю: значок вплотную к границе на рабочем столе выглядит
    # больше соседних и «наезжает» на подпись.
    m, radius = 0.04, 0.18

    # Сначала — один проход по сэмплам, затем усреднение блоками. Держать
    # промежуточный буфер дешевле, чем считать фигуры по SS×SS раз на пиксель.
    samples = bytearray(n * n * 4)
    for sy in range(n):
        y = (sy + 0.5) / n
        row = sy * n * 4
        for sx in range(n):
            x = (sx + 0.5) / n
            i = row + sx * 4
            if not _rounded_rect(x, y, m, m, 1 - m, 1 - m, radius):
                continue                      # прозрачно, буфер уже в нулях
            if glyph(x, y):
                samples[i] = wr
                samples[i + 1] = wg
                samples[i + 2] = wb
            else:
                samples[i] = br
                samples[i + 1] = bgc
                samples[i + 2] = bb
            samples[i + 3] = 255

    out = bytearray(size * size * 4)
    for py in range(size):
        # BMP хранит строки СНИЗУ ВВЕРХ. Перепутать это — значок вверх ногами,
        # и заметно это только на готовом файле в проводнике.
        dst = (size - 1 - py) * size * 4
        for px in range(size):
            sa = sr = sg = sb = 0
            for dy in range(SS):
                base = ((py * SS + dy) * n + px * SS) * 4
                for dx in range(SS):
                    i = base + dx * 4
                    a = samples[i + 3]
                    if a:
                        # Складываем ПОМНОЖЕННЫЕ на альфу цвета: иначе на краю
                        # скругления прозрачные сэмплы (нулевые по цвету) тянут
                        # среднее к чёрному, и по контуру идёт тёмный ореол.
                        sr += samples[i] * a
                        sg += samples[i + 1] * a
                        sb += samples[i + 2] * a
                        sa += a
            j = dst + px * 4
            if sa:
                out[j] = sr // sa
                out[j + 1] = sg // sa
                out[j + 2] = sb // sa
                out[j + 3] = sa // (SS * SS)
    return bytes(out)


def build_ico(frames: list[tuple[int, bytes]]) -> bytes:
    """Собрать `.ico` из готовых BGRA-кадров."""
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries, blobs = [], []
    for size, pixels in frames:
        mask_row = ((size + 31) // 32) * 4      # 1 бит на пиксель, строка до 4 байт
        mask = b"\x00" * (mask_row * size)
        info = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                           len(pixels) + len(mask), 0, 0, 0, 0)
        blob = info + pixels + mask
        # 256 записывается нулём: поле однобайтовое, и 256 в него не влезает.
        byte = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", byte, byte, 0, 0, 1, 32,
                                   len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    return header + b"".join(entries) + b"".join(blobs)


def icon_bytes(name: str, sizes=SIZES) -> bytes:
    bg, glyph = ICONS[name]
    return build_ico([(s, render(s, bg, glyph)) for s in sizes])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy"))
    a = ap.parse_args()
    for name in sorted(ICONS):
        path = os.path.join(a.out_dir, name + ".ico")
        data = icon_bytes(name)
        with open(path, "wb") as f:
            f.write(data)
        print(f"{path}: {len(data)} байт, кадры {', '.join(map(str, SIZES))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
