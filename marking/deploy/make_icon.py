"""Иконка программы «Маркировка и поставки»: deploy/marking.ico и favicon.

Синий квадрат с белой «наклейкой» и узором DataMatrix — тем самым кодом
маркировки, ради которого программа и есть. Цвета — из style.css (--accent и
тёмная шапка), чтобы ярлык и страница выглядели одной программой.

Каждый размер рисуется САМ, по целым пикселям, а не уменьшением большой
картинки: уменьшенный узор на 16–32 px превращается в серое пятно, и ярлык на
панели задач не отличить от соседних. На мелких размерах модулей меньше
(6×6 вместо 10×10), но каждый модуль — целое число пикселей.

Запуск (нужен Pillow): python deploy/make_icon.py — перезаписывает оба файла.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICO = ROOT / "deploy" / "marking.ico"
FAVICON = ROOT / "markapp" / "static" / "favicon.ico"

BLUE = (36, 87, 197, 255)      # --accent
DARK = (29, 35, 48, 255)       # шапка страниц
WHITE = (255, 255, 255, 255)
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# Узор внутри рамки DataMatrix. Не настоящий код — просто узнаваемый рисунок;
# фиксированный, чтобы иконка не менялась от сборки к сборке.
INNER_8 = [
    "10110010",
    "01101101",
    "11010110",
    "00111001",
    "10100111",
    "01011010",
    "11100101",
    "00101110",
]
INNER_4 = ["1011", "0110", "1101", "0101"]


def matrix(n: int) -> list[list[int]]:
    """n×n DataMatrix: сплошная «L» слева и снизу, пунктир сверху и справа."""
    inner = INNER_8 if n == 10 else INNER_4
    m = [[0] * n for _ in range(n)]
    for i in range(n):
        m[i][0] = 1                       # левый край — сплошной
        m[n - 1][i] = 1                   # нижний край — сплошной
        m[0][i] = 1 if i % 2 == 0 else 0  # верх — пунктир
        m[i][n - 1] = 1 if (n - 1 - i) % 2 == 0 else 0   # правый — пунктир
    for r in range(n - 2):
        for c in range(n - 2):
            m[r + 1][c + 1] = int(inner[r][c])
    return m


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = max(2, round(size * 0.18))
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BLUE)

    n = 10 if size >= 40 else 6
    module = max(1, int(size * 0.66) // n)
    if size <= 20:
        module = 2
    code = n * module
    # Белое поле вокруг кода. На 16–20 px его нет: каждый пиксель поля
    # съедает синюю рамку, и иконка становится просто белым квадратом.
    pad = module if size >= 32 else (1 if size >= 24 else 0)
    label = code + 2 * pad
    x0 = (size - label) // 2
    y0 = (size - label) // 2
    lr = max(0, round(label * 0.08)) if size >= 32 else 0
    d.rounded_rectangle((x0, y0, x0 + label - 1, y0 + label - 1), radius=lr, fill=WHITE)

    cx, cy = x0 + pad, y0 + pad
    for r, row in enumerate(matrix(n)):
        for c, bit in enumerate(row):
            if bit:
                d.rectangle((cx + c * module, cy + r * module,
                             cx + (c + 1) * module - 1, cy + (r + 1) * module - 1), fill=DARK)
    return img


def build() -> list[Image.Image]:
    return [draw(s) for s in SIZES]


def save(path: Path) -> None:
    images = build()
    big = images[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    big.save(path, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])


if __name__ == "__main__":
    save(ICO)
    save(FAVICON)
    print(f"{ICO}\n{FAVICON}")
