"""Геометрия графиков дашборда — считается здесь, шаблон только рисует SVG.

Без JS-библиотек (как и всё приложение): столбики с накоплением по площадкам
строятся сервером, всплывающая подсказка — несколько строк ванильного JS в
шаблоне. Цвет закреплён за площадкой (не за её местом в списке): фильтр,
убравший площадку, не перекрашивает остальные.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# Категориальная палитра (проверена валидатором на белом фоне: CVD ΔE ≥ 9,
# различимость ≥ 22). Аква и жёлтый ниже 3:1 по контрасту — поэтому у графика
# всегда есть легенда и таблица с теми же числами.
PLATFORM_COLORS = {
    "wb": "#2a78d6",
    "lamoda": "#eb6834",
    "kit": "#1baf7a",
    "ozon": "#eda100",
}

WIDTH = 960
HEIGHT = 260
PAD_LEFT = 64
PAD_RIGHT = 16
PAD_TOP = 12
PAD_BOTTOM = 28
BAR_MAX = 24        # толщина столбика не больше 24px — остальное воздух
GAP = 2             # зазор цвета фона между сегментами стопки
RADIUS = 4          # скругление только у вершины столбика


@dataclass
class Segment:
    platform: str
    x: float
    y: float
    w: float
    h: float
    path: str
    color: str


@dataclass
class Column:
    start: date
    label: str
    x: float
    cx: float
    hit_x: float
    hit_w: float
    segments: list[Segment] = field(default_factory=list)
    values: dict[str, int] = field(default_factory=dict)   # копейки, как есть (могут быть < 0)
    total: int = 0


def _nice_step(max_value: float, ticks: int = 4) -> float:
    if max_value <= 0:
        return 1
    raw = max_value / ticks
    magnitude = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    for mult in (1, 2, 2.5, 5, 10):
        if raw <= mult * magnitude:
            return mult * magnitude
    return 10 * magnitude


def compact_rub(rub: float) -> str:
    """Подпись оси: 0 / 500 тыс / 1,2 млн."""
    value = abs(rub)
    sign = "−" if rub < 0 else ""
    if value >= 1_000_000:
        text = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + " млн"
    elif value >= 1_000:
        text = f"{value / 1_000:.0f} тыс"
    else:
        text = f"{value:.0f}"
    return sign + text.replace(".", ",")


def _top_rounded(x: float, y: float, w: float, h: float, r: float) -> str:
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def _rect(x: float, y: float, w: float, h: float) -> str:
    return f"M{x:.1f},{y:.1f} H{x + w:.1f} V{y + h:.1f} H{x:.1f} Z"


def stacked_columns(buckets: list[tuple[date, str, int]], platforms: list[str],
                    bucket: str) -> dict:
    """buckets: [(начало периода, площадка, значение в копейках)].

    Отрицательные значения (возвратов больше, чем продаж) в стопку не кладутся —
    у стопки одна ось от нуля; точное число остаётся в подсказке и таблице."""
    starts = sorted({b[0] for b in buckets})
    by_key = {(b[0], b[1]): b[2] for b in buckets}
    stacks = [sum(max(by_key.get((s, p), 0), 0) for p in platforms) for s in starts]
    max_rub = max(stacks, default=0) / 100
    step = _nice_step(max_rub)
    top = step * max(1, -(-max_rub // step)) if max_rub else step * 4
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT

    def y_of(rub: float) -> float:
        return PAD_TOP + plot_h - (rub / top) * plot_h

    band = plot_w / max(len(starts), 1)
    bar_w = min(BAR_MAX, band * 0.6)
    columns = []
    for i, start in enumerate(starts):
        cx = PAD_LEFT + band * i + band / 2
        x = cx - bar_w / 2
        label = f"{start:%m.%Y}" if bucket == "month" else f"{start:%d.%m}"
        col = Column(start=start, label=label, x=x, cx=cx, hit_x=PAD_LEFT + band * i, hit_w=band)
        positive = [(p, by_key.get((start, p), 0)) for p in platforms]
        drawn = [(p, val) for p, val in positive if val > 0]
        base = 0.0
        for idx, (platform, val) in enumerate(drawn):
            rub = val / 100
            y_top = y_of(base + rub)
            y_bottom = y_of(base)
            h = y_bottom - y_top
            # Зазор 2px цвета фона между сегментами — срезаем низ у всех, кроме
            # нижнего, вместо обводки.
            if idx > 0:
                h = max(h - GAP, 0.5)
            is_top = idx == len(drawn) - 1
            path = _top_rounded(x, y_top, bar_w, h, RADIUS) if is_top else _rect(x, y_top, bar_w, h)
            col.segments.append(Segment(platform, x, y_top, bar_w, h, path,
                                        PLATFORM_COLORS.get(platform, "#898781")))
            base += rub
        col.values = {p: by_key.get((start, p), 0) for p in platforms if (start, p) in by_key}
        col.total = sum(col.values.values())
        columns.append(col)

    ticks = []
    value = 0.0
    while value <= top + 1e-9:
        ticks.append({"y": round(y_of(value), 1), "label": compact_rub(value)})
        value += step
    # Подписи под каждым столбиком при 50 неделях слипались бы — оставляем
    # каждую n-ю (полный список дат — в таблице).
    every = max(1, -(-len(columns) // 12))
    return {
        "width": WIDTH, "height": HEIGHT, "pad_left": PAD_LEFT, "pad_right": PAD_RIGHT,
        "baseline": round(y_of(0), 1), "columns": columns, "ticks": ticks,
        "label_every": every, "label_y": HEIGHT - 8,
    }


def meter_width(share: float | None) -> float:
    """Ширина заливки индикатора доли (0–100 %), обрезанная по краям."""
    if share is None:
        return 0
    return float(max(Decimal(0), min(Decimal(100), Decimal(str(share * 100)))))
