"""Скрипты установки (BOM, не трогают чужие службы) и обработка 1С (часть mark-3).

Проверки модуля 1С — про исходник в репозитории (`../1c/`); на офисном
компьютере его рядом нет, и они пропускаются.
"""
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
DEPLOY = HERE / "deploy"
MODULE = HERE.parent / "1c" / "ОбменССайтом_МодульОбъекта.txt"
repository_only = pytest.mark.skipif(not MODULE.exists(), reason="исходник 1С есть только в репозитории")


def test_every_russian_ps1_has_a_bom():
    scripts = list(DEPLOY.glob("*.ps1"))
    assert scripts
    for p in scripts:
        raw = p.read_bytes()
        text = raw.decode("utf-8-sig")
        if any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text):
            assert raw.startswith(b"\xef\xbb\xbf"), p.name


def test_scripts_never_touch_other_programs():
    for p in DEPLOY.glob("*.ps1"):
        text = p.read_text(encoding="utf-8-sig")
        for svc in ("sync_admin_web", "sync_admin_worker"):
            assert svc not in text, p.name
        assert "markapp.main:app" not in text, p.name


def _function(name: str) -> str:
    text = MODULE.read_text(encoding="utf-8")
    m = re.search(rf"\n(Функция|Процедура) {name}\(.*?\n(КонецФункции|КонецПроцедуры)", text, re.S)
    assert m, name
    return m.group(0)


@repository_only
def test_price_answers_go_to_their_own_folder():
    f = _function("КаталогОтветов")
    assert 'СтрНачинаетсяС(Метка, "price_")' in f and '"pricing"' in f
    assert 'СтрНачинаетсяС(Метка, "mark_")' in f and '"marking"' in f


@repository_only
def test_module_version_is_mark_3_or_later():
    assert int(re.search(r'Возврат "mark-(\d+)";', _function("ВерсияМодуля")).group(1)) >= 3


@repository_only
def test_cost_file_is_published_before_the_answer():
    text = MODULE.read_text(encoding="utf-8")
    cost = text.index('"cost_" + Метка')
    result = text.index('"result_" + Метка')
    assert cost < result, "программа, увидев result, сразу ищет рядом cost — файл обязан уже лежать"
    assert 'КаталогДляОтветов, "cost_" + Метка' in text


@repository_only
def test_cost_source_is_the_cost_register_only():
    f = _function("ВыгрузитьСебестоимость")
    assert "Пар.РегистрСебестоимости" in f and ".СрезПоследних" in f
    assert "ЦеныНоменклатуры" not in f and "Партии" not in f
    assert 'Пар.Вставить("РегистрСебестоимости", "СебестоимостьНоменклатуры")' in MODULE.read_text(encoding="utf-8")


@repository_only
def test_cost_command_checks_field_count_and_answers_with_its_name():
    text = MODULE.read_text(encoding="utf-8")
    block = text[text.index('ИначеЕсли Команда = "EXPORT_COST_PRICES"'):]
    block = block[:block.index("ИначеЕсли", 10)]
    assert "Поля.Количество() < 2" in block
    assert block.count("|EXPORT_COST_PRICES\"") == 2


# --- значок и ярлык -------------------------------------------------------------

def _gen_icon():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_icon", HERE / "scripts" / "gen_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_icons_are_rebuilt_byte_for_byte_from_the_generator():
    """Значок — арифметика в scripts/gen_icon.py; лежащий файл обязан ей совпадать,
    иначе правка генератора молча не дошла бы до ярлыка."""
    gen = _gen_icon()
    for path, sizes in gen.OUTPUTS:
        assert Path(path).read_bytes() == gen.icon_bytes(sizes), path


def test_icon_has_the_frames_windows_needs():
    import struct
    data = (DEPLOY / "repricer.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 4)
    sizes = sorted(256 if data[6 + 16 * i] == 0 else data[6 + 16 * i] for i in range(count))
    assert sizes == [16, 32, 48, 256]


def test_shortcut_uses_own_icon_and_silent_launcher():
    s = (DEPLOY / "create_shortcut.ps1").read_text(encoding="utf-8-sig")
    assert "repricer.ico" in s and "IconLocation" in s and "wscript.exe" in s and "launch_repricer.vbs" in s
    for name in ("install_workstation.ps1", "update_workstation.ps1"):
        assert "create_shortcut.ps1" in (DEPLOY / name).read_text(encoding="utf-8-sig"), name
    assert "-RefreshOnly" in (DEPLOY / "update_workstation.ps1").read_text(encoding="utf-8-sig")
    vbs = (DEPLOY / "launch_repricer.vbs").read_bytes()
    vbs.decode("ascii")      # wscript читает .vbs в ANSI-кодировке — только ASCII
    assert b"run_repricer.ps1" in vbs and b", 0, False" in vbs


def test_favicon_is_served_and_linked(client):
    assert client.get("/static/favicon.ico").status_code == 200
    assert 'href="/static/favicon.ico"' in client.get("/attention").text


def test_no_drive_colon_trap_in_ps1():
    """`"$User: текст"` PowerShell 5.1 разбирает как обращение к диску «User:» и
    падает ДО первой команды. Здесь (Linux) это не видно ничем, кроме этого теста."""
    bad = []
    for p in DEPLOY.glob("*.ps1"):
        for n, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
            for m in re.finditer(r'\$([A-Za-z_]\w*):', line):
                if m.group(1).lower() not in ("env", "script", "global", "local", "private", "using"):
                    bad.append(f"{p.name}:{n}: {line.strip()}")
    assert not bad, bad


def test_server_script_only_adds_folders_and_a_key():
    s = (DEPLOY / "server_add_repricer.ps1").read_text(encoding="utf-8-sig")
    assert r"results\pricing" in s and r"archive\pricing" in s and "AppendAllText" in s
    assert "sshd_config" not in s.replace("sshd_config, порт", "")   # конфиг SSH не трогает
    assert "Restart-Service" not in s and "WriteAllText" not in s      # ключ «Маркировки» не затирается
