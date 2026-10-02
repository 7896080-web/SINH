"""Иконка ярлыка и вкладки браузера."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICO = ROOT / "deploy" / "marking.ico"


def test_icon_has_every_size_windows_asks_for():
    """Без 16/32/48 Windows растягивает 256 — узор на панели задач становится пятном."""
    sizes = set(Image.open(ICO).info["sizes"])
    assert {(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)} <= sizes


def test_icon_matches_its_generator():
    """Файл собран тем же генератором, что лежит рядом: правка рисунка без
    пересборки (или наоборот) иначе разошлась бы молча."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "deploy" / "make_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = {img.size: img for img in mod.build()}
    ico = Image.open(ICO)
    for size in ((16, 16), (32, 32), (48, 48)):
        ico.size = size
        got = ico.convert("RGBA")
        assert got.tobytes() == expected[size].tobytes(), size


def test_installer_and_updater_put_the_icon_on_the_shortcut():
    for name in ("install_workstation.ps1", "update_workstation.ps1"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8-sig")
        assert 'Join-Path $Root "deploy\\marking.ico"' in text, name
        assert '.IconLocation = "$icon,0"' in text, name


def test_updater_touches_only_the_icon():
    """Обновление не пересоздаёт ярлык: путь запуска и порт в нём — дело установщика."""
    text = (ROOT / "deploy" / "update_workstation.ps1").read_text(encoding="utf-8-sig")
    block = text[text.index("Иконка на уже созданных ярлыках"):text.index('Info "Запуск"')]
    assert "TargetPath" not in block and "Arguments" not in block


def test_favicon_is_served_and_linked(client):
    r = client.get("/static/favicon.ico")
    assert r.status_code == 200 and r.content[:4] == b"\x00\x00\x01\x00"
    assert (ROOT / "markapp" / "static" / "favicon.ico").read_bytes() == ICO.read_bytes()
    base = (ROOT / "markapp" / "templates" / "base.html").read_text(encoding="utf-8")
    assert '<link rel="icon" href="/static/favicon.ico">' in base


def test_icon_refresh_cannot_abort_the_update():
    """Сбой обновления иконки — предупреждение: при Stop он оборвал бы
    обновление после остановки программы, и она осталась бы не запущенной."""
    text = (ROOT / "deploy" / "update_workstation.ps1").read_text(encoding="utf-8-sig")
    block = text[text.index("Иконка на уже созданных ярлыках"):text.index('Info "Запуск"')]
    assert "try {" in block and "} catch {" in block
    assert block.index("try {") < block.index("CreateShortcut")
