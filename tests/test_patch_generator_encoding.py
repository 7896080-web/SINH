"""Патч-файл наката обязан рождаться с BOM.

Windows PowerShell 5.1 считает, что `.ps1` без BOM написан в системной кодировке
(cp1251), и кириллица в комментариях и сообщениях превращается в мусор. Беда не
в нечитаемости: длинное тире «—» — это байты E2 80 94, в cp1251 они читаются как
«вЂ"», и последний символ ЗАКРЫВАЕТ строковый литерал раньше времени. Парсер
падает на «непредвиденная лексема», не дойдя до блоков с данными.

21.09 на бою так и вышло при запуске патча файлом. До этого блоки вставляли в
консоль руками — там кодировка уже верная, поэтому дефект дожил до первого
запуска файлом и проявился на боевом сервере, а не в разработке.
"""
import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _generate(tmp_path, files):
    out = tmp_path / "probe_deploy.ps1"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_whole_file_patch.py"),
         "--tag", "probe", "--parts", "1", "--out", str(out), *files],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    return out


def test_the_patch_file_starts_with_a_bom(tmp_path):
    out = _generate(tmp_path, ["README.md"])
    assert out.read_bytes()[:3] == b"\xef\xbb\xbf", (
        "без BOM PowerShell 5.1 прочитает файл как cp1251 и упадёт на разборе")


def test_the_patch_keeps_windows_line_endings(tmp_path):
    """Файл едет на Windows и правится там блокнотом; LF-переводы строк в нём —
    источник отдельных сюрпризов."""
    out = _generate(tmp_path, ["README.md"])
    raw = out.read_bytes()
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0, "остались одиночные LF"


def test_the_dash_that_broke_the_parser_is_still_there(tmp_path):
    """Тест охраняет не абстрактный BOM, а конкретную поломку: если из шаблона
    однажды уберут кириллицу и тире, проверка выше станет бессмысленной, и никто
    этого не заметит."""
    out = _generate(tmp_path, ["README.md"])
    text = io.open(out, encoding="utf-8-sig").read()
    assert "—" in text, "длинное тире — та самая последовательность E2 80 94"
    assert any("а" <= ch <= "я" for ch in text), "кириллица в патче"
