"""Установка на сервер: скрипты PowerShell и пакет программы."""
import base64
import hashlib
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.mark.parametrize("name", ["install_server.ps1", "update_server.ps1", "create_shortcut.ps1"])
def test_powershell_scripts_have_bom_and_crlf(name):
    """Windows PowerShell 5.1 читает .ps1 без BOM как cp1251: кириллица в
    сообщениях ломается, а «—» закрывает строку раньше времени (урок sync_admin)."""
    data = (ROOT / "deploy" / name).read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_scripts_use_our_names_not_another_programs():
    for name in ("install_server.ps1", "update_server.ps1"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8-sig")
        assert "finapp.main:app" in text or "finapp" in text
        assert "priceapp" not in text and "repricer" not in text.lower()
        assert '"mpfinance"' in text and "8003" in text


IN_REPOSITORY = (ROOT.parent / ".git").exists()


@pytest.mark.skipif(not IN_REPOSITORY, reason="пакет собирается из git — только в репозитории")
def test_package_lays_out_every_release_file(tmp_path):
    import gen_package

    target = tmp_path / "mpfinance"
    text, info = gen_package.build(str(ROOT), "fintest", str(target))
    body = text.split("$b = @'\n", 1)[1].split("\n'@", 1)[0]
    b64 = re.sub(r"\s", "", body)
    apply_src = zlib.decompress(base64.b64decode(b64)).decode("utf-8")
    script = tmp_path / "apply.py"
    script.write_text(apply_src, encoding="utf-8")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=True).stdout
    assert f"DONE {info['files']}" in out
    assert (target / "finapp" / "main.py").read_bytes() == (ROOT / "finapp" / "main.py").read_bytes()
    assert (target / "deploy" / "INSTALLED_TAG").read_text(encoding="utf-8").startswith("fintest")
    # Повторный прогон ничего не переписывает.
    again = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=True).stdout
    assert "DONE 0" in again


def test_desktop_icon_is_a_valid_ico_built_by_the_generator():
    """Значок — то, что собирает `scripts/gen_icon.py`, байт в байт: правка
    генератора без пересборки файла (или наоборот) не прошла бы молча."""
    import struct

    import gen_icon

    data = (ROOT / "deploy" / "mpfinance.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = sorted((data[6 + 16 * i] or 256) for i in range(count))
    assert sizes == [16, 32, 48, 256]
    assert data == gen_icon.icon_bytes()


def test_icon_glyph_has_a_notch_and_differs_from_a_plain_disc():
    """Вырез сектора виден: точка в вынутой четверти у края круга — фон, а
    сам сектор сдвинут наружу и есть."""
    import gen_icon

    assert gen_icon.glyph_pie(0.5, 0.5 + 0.2)          # низ круга
    assert not gen_icon.glyph_pie(0.52, 0.48)          # щель у центра — вырез
    assert gen_icon.glyph_pie(0.5 + 0.18, 0.5 - 0.18)  # сдвинутая четверть
    assert not gen_icon.glyph_pie(0.05, 0.05)


def test_shortcut_gets_our_icon_on_install_and_on_update():
    shortcut = (ROOT / "deploy" / "create_shortcut.ps1").read_text(encoding="utf-8-sig")
    assert 'deploy\\mpfinance.ico' in shortcut and "IconLocation" in shortcut
    assert "chrome.exe" in shortcut and "msedge.exe" in shortcut      # браузер явно, не IE
    assert "probe" in shortcut                                        # право записи — записью
    for name in ("install_server.ps1", "update_server.ps1"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8-sig")
        assert "create_shortcut.ps1" in text, name


# ------------------------------------------------------------ патч для вставки в консоль

def _repo(tmp_path):
    """Маленький «релиз» в своём git: версия A (как на сервере) и правки после."""
    root = tmp_path / "repo"
    (root / "finapp").mkdir(parents=True)
    (root / "finapp" / "a.py").write_text("print('A')\n", encoding="utf-8")
    (root / "finapp" / "b.py").write_text("# неизменный\n", encoding="utf-8")
    (root / "deploy").mkdir()
    (root / "deploy" / "x.ps1").write_bytes("﻿Write-Host 'А'\r\n".encode("utf-8"))
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "A"], check=True)
    base = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    server = tmp_path / "server"
    shutil.copytree(root, server, ignore=shutil.ignore_patterns(".git"))
    (server / "deploy" / "INSTALLED_TAG").write_text("finA\n", encoding="utf-8")
    (root / "finapp" / "a.py").write_text("print('B — новое')\n", encoding="utf-8")
    (root / "finapp" / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    (root / "deploy" / "x.ps1").write_bytes("﻿Write-Host 'Б'\r\n".encode("utf-8"))
    return root, base, server


def _apply_source(text: str) -> str:
    """Собрать base64 из блоков так же, как БЛОК APPLY, и распаковать."""
    import lzma
    parts = re.findall(r"^\$m\d+ = @'\n(.*?)\n'@", text, re.S | re.M)
    b64 = "".join(re.sub(r"\s", "", p) for p in parts)
    assert hashlib.sha256(b64.encode()).hexdigest() in text           # полный SHA напечатан для сверки
    return lzma.decompress(base64.b64decode(b64)).decode("utf-8")


def _run_apply(tmp_path, source):
    script = tmp_path / "apply.py"
    script.write_text(source, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def test_paste_patch_carries_only_what_changed_and_lands_byte_for_byte(tmp_path):
    import gen_package
    root, base, server = _repo(tmp_path)
    text, info = gen_package.build_patch(str(root), "finB", base, "finA", str(server))
    assert info["files"] == ["deploy/x.ps1", "finapp/a.py", "finapp/new.py"]
    assert "БЛОК APPLY" in text and text.count("---- БЛОК ") == info["parts"] + 1
    done = _run_apply(tmp_path, _apply_source(text))
    assert done.returncode == 0, done.stdout + done.stderr
    for rel in ("finapp/a.py", "finapp/new.py", "deploy/x.ps1", "finapp/b.py"):
        assert (server / rel).read_bytes() == (root / rel).read_bytes()         # BOM и CRLF целы
    assert (server / "finapp" / "a.py.bak_finB").read_text(encoding="utf-8") == "print('A')\n"
    assert (server / "deploy" / "INSTALLED_TAG").read_text(encoding="utf-8").startswith("finB")
    again = _run_apply(tmp_path, _apply_source(text))
    assert again.returncode == 0 and "DONE 0" in again.stdout                  # повтор ничего не пишет


def test_paste_patch_refuses_another_version_and_touches_nothing(tmp_path):
    import gen_package
    root, base, server = _repo(tmp_path)
    text, _ = gen_package.build_patch(str(root), "finB", base, "finA", str(server))
    (server / "deploy" / "INSTALLED_TAG").write_text("fin3\n", encoding="utf-8")
    done = _run_apply(tmp_path, _apply_source(text))
    assert done.returncode != 0 and "ничего не тронуто" in done.stderr
    assert (server / "finapp" / "a.py").read_text(encoding="utf-8") == "print('A')\n"
    assert not (server / "finapp" / "new.py").exists()


def test_paste_patch_does_not_start_over_a_hand_edited_file(tmp_path):
    """Сверка до первой записи: файл, правленный руками, не перезаписывается, и
    остальные тоже не пишутся — половина патча хуже, чем ни одного."""
    import gen_package
    root, base, server = _repo(tmp_path)
    text, _ = gen_package.build_patch(str(root), "finB", base, "finA", str(server))
    (server / "deploy" / "x.ps1").write_text("правка руками", encoding="utf-8")
    done = _run_apply(tmp_path, _apply_source(text))
    assert done.returncode != 0 and "deploy/x.ps1 (не та версия)" in done.stdout
    assert (server / "finapp" / "a.py").read_text(encoding="utf-8") == "print('A')\n"
    assert not (server / "finapp" / "new.py").exists()


def test_paste_patch_is_refused_when_a_file_was_deleted(tmp_path):
    import gen_package
    root, base, server = _repo(tmp_path)
    (root / "finapp" / "b.py").unlink()
    with pytest.raises(SystemExit, match="finapp/b.py"):
        gen_package.build_patch(str(root), "finB", base, "finA", str(server))
