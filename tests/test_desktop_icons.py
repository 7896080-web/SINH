"""Значки ярлыков на рабочем столе.

Ярлыка на столе два, и стоят они рядом. С иконкой браузера это два одинаковых
Chrome: какой ведёт в админку, а какой в возвраты, видно только по подписи — а
в панели задач подписи нет вовсе. Поэтому значки свои, и главное их свойство
не «красиво», а **различимы**: разной ФОРМЫ, а не только цвета. По RDP, где
люди и работают с этим сервером, цвет сжимается сильнее всего.

Файл `.ico` двоичный, то есть по нему ничего не прочитать и глазами его не
проверить. Отсюда два разряда проверок. Первый — файл совпадает с тем, что
даёт `scripts/gen_icons.py`: разойдись они, картинку стало бы нечем
перерисовать, и «откуда она взялась» перестало бы иметь ответ. Второй — сам
файл устроен так, как ждёт Windows: неверный `.ico` не даёт ошибки, значок
просто не рисуется, и выглядит это как невыполненный скрипт.
"""
import struct
from pathlib import Path

import pytest

from scripts import gen_icons

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


def _parse(data: bytes):
    """Разобрать `.ico` в список кадров: (сторона, бит на пиксель, пиксели)."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, "это не .ico"
    frames = []
    for i in range(count):
        head = data[6 + 16 * i:22 + 16 * i]
        w, h, colors, pad, planes, bits, length, offset = struct.unpack("<BBBBHHII", head)
        side = w or 256                      # 0 значит 256: поле однобайтовое
        assert h == w, "кадры квадратные"
        assert colors == 0 and pad == 0 and planes == 1
        blob = data[offset:offset + length]
        assert len(blob) == length, "кадр обрезан — файл собран неверно"
        info = struct.unpack("<IiiHHIIiiII", blob[:40])
        assert info[0] == 40, "не BITMAPINFOHEADER"
        assert info[1] == side, "ширина в заголовке кадра разошлась с описью"
        # Высота ВДВОЕ больше: следом за картинкой идёт маска прозрачности.
        # Забыть это удвоение — значок, растянутый вдвое и наполовину пустой.
        assert info[2] == side * 2, "высота обязана учитывать маску"
        assert info[5] == 0, "сжатия внутри .ico быть не должно"
        frames.append((side, info[4], blob[40:40 + side * side * 4]))
    return frames


def _icons():
    found = sorted(DEPLOY.glob("*.ico"))
    assert found, "значков нет вовсе — ярлыки останутся с иконкой браузера"
    return found


@pytest.mark.parametrize("path", _icons(), ids=lambda p: p.name)
def test_an_icon_is_a_valid_windows_icon(path):
    """Битый `.ico` не даёт ошибки — значок просто не рисуется.

    Это ровно тот немой отказ, которого в этом проекте боятся больше явного:
    человек запускает скрипт, видит «Создан ярлык», а на столе пусто — и решает,
    что сломан скрипт.
    """
    frames = _parse(path.read_bytes())
    sides = [f[0] for f in frames]
    assert sides == sorted(sides), "кадры должны идти по возрастанию"
    assert set(sides) == set(gen_icons.SIZES), (
        f"{path.name}: кадры {sides}, а ожидались {list(gen_icons.SIZES)}")
    assert 16 in sides, "16 — это панель задач, без него Windows уменьшит 32-й"
    for side, bits, pixels in frames:
        assert bits == 32, "нужен альфа-канал, иначе у значка чёрный угол"
        assert len(pixels) == side * side * 4


@pytest.mark.parametrize("path", _icons(), ids=lambda p: p.name)
def test_an_icon_is_exactly_what_the_generator_draws(path):
    """Файл в репозитории — ровно то, что даёт генератор.

    Разойдись они, `gen_icons.py` остался бы описанием картинки, которой нет:
    перекрасить значок было бы нечем, а «откуда он взялся» снова стало бы
    вопросом без ответа. Сравнение побайтовое и по ВСЕМ кадрам, включая 256:
    проверять дешёвые кадры и верить на слово дорогому значило бы оставить
    самый заметный без присмотра.
    """
    name = path.stem
    assert name in gen_icons.ICONS, (
        f"{path.name} не описан в gen_icons.ICONS — пересобрать его нечем")
    assert path.read_bytes() == gen_icons.icon_bytes(name), (
        f"{path.name} разошёлся с генератором; пересоберите: "
        "python3 scripts/gen_icons.py")


def _silhouette(path: Path, side: int = 16) -> frozenset:
    """Множество белых точек кадра — то, что человек и видит как рисунок."""
    for s, _bits, pixels in _parse(path.read_bytes()):
        if s != side:
            continue
        out = set()
        for i in range(side * side):
            b, g, r, a = pixels[i * 4:i * 4 + 4]
            if a > 128 and min(r, g, b) > 200:
                out.add(i)
        return frozenset(out)
    raise AssertionError(f"в {path.name} нет кадра {side}")


@pytest.mark.parametrize("path", _icons(), ids=lambda p: p.name)
def test_an_icon_actually_has_a_drawing_on_it(path):
    """Значок без рисунка — цветной квадрат, и два таких неразличимы совсем.

    Границы широкие намеренно: проверка не про красоту, а про то, что глиф не
    исчез и не разросся на весь фон (и то, и другое даёт ровно одно пятно).
    """
    white = len(_silhouette(path))
    assert 20 <= white <= 180, (
        f"{path.name}: белых точек в кадре 16 — {white}; рисунка либо нет, "
        "либо он залил весь значок")


def test_the_icons_differ_by_shape_and_not_only_by_colour():
    """Главное свойство набора, и единственное, ради чего он заведён.

    Перекрасить один и тот же глиф в два цвета — самая естественная ошибка тут,
    и на глаз в репозитории она незаметна: файлы разные, размеры разные, всё
    выглядит сделанным. А на рабочем столе по RDP получилось бы два одинаковых
    значка разного оттенка — то есть ровно то, от чего уходили.
    """
    shapes = {p.stem: _silhouette(p) for p in _icons()}
    names = sorted(shapes)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = shapes[a] & shapes[b]
            union = shapes[a] | shapes[b]
            overlap = len(common) / len(union)
            assert overlap < 0.6, (
                f"силуэты {a} и {b} совпадают на {overlap:.0%} — с трёх шагов "
                "это один и тот же значок")


def test_every_icon_the_shortcut_script_names_exists():
    """Ссылка на отсутствующий файл — тот же немой отказ.

    Скрипт об этом говорит вслух на сервере, но узнать о нём там значит узнать
    последним: опечатку в имени видно отсюда, и стоит она одной строки.
    """
    text = (DEPLOY / "create_desktop_shortcut.ps1").read_text(encoding="utf-8-sig")
    named = set()
    for chunk in text.split('"'):
        if chunk.endswith(".ico"):
            named.add(chunk.strip())
    assert named, "скрипт не называет ни одного значка — ярлыки будут с иконкой браузера"
    for icon in sorted(named):
        assert (DEPLOY / icon).exists(), f"скрипт зовёт {icon}, а файла нет"
