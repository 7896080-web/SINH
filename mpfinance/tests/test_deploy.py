"""Установка на сервер: скрипты PowerShell и пакет программы."""
import base64
import re
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.mark.parametrize("name", ["install_server.ps1", "update_server.ps1"])
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
