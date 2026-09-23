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


# ---------------------------------------------------------------------------
# Накат обязан оставлять след «какая версия тут лежит»
# ---------------------------------------------------------------------------

def _apply_source(ps1: Path) -> str:
    """Достать из .ps1 тот самый python-скрипт, который выполнится на сервере."""
    import base64 as b64mod
    import re
    import zlib

    text = ps1.read_text(encoding="utf-8-sig")
    pieces = re.findall(r"@'\n(.*?)\n'@", text, re.S)
    joined = "".join("".join(p.split()) for p in pieces)
    return zlib.decompress(b64mod.b64decode(joined)).decode("utf-8")


def test_the_patch_records_which_version_it_installed(tmp_path):
    """23.09 на бою: БЛОК APPLY не запускали вовсе.

    `update_windows.ps1` честно отработал на СТАРОМ коде и закончился зелёным —
    копия базы снята, `pytest -q` зелёный (код и база друг другу соответствуют),
    `/health` 200, все задания свежие. Единственным следом того, что новой версии
    на сервере нет, было ОТСУТСТВИЕ строки «Running upgrade» в логе миграций, то
    есть признак, которого никто не ищет. Человек при этом уверен, что поставил
    новую версию, и уходит.

    Прогоняем сам apply-скрипт (с подменённым корнем) и требуем отметку: она
    единственное, по чему второй шаг наката может узнать про первый.
    """
    out = _generate(tmp_path, ["README.md"])

    apply_py = _apply_source(out)
    target = tmp_path / "server"
    (target / "deploy").mkdir(parents=True)
    apply_py = apply_py.replace('ROOT = r"C:\\sync_admin"', f"ROOT = r{str(target)!r}")
    script = tmp_path / "apply.py"
    script.write_text(apply_py, encoding="utf-8")

    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "DONE" in r.stdout

    marker = target / "deploy" / "INSTALLED_TAG"
    assert marker.exists(), "накат не оставил следа — второй шаг о нём не узнает"
    assert marker.read_text(encoding="utf-8").startswith("probe"), (
        "в отметке обязана стоять метка патча, иначе сверять не с чем")
    assert (target / "README.md").exists(), "файлы релиза должны были записаться"


def test_the_update_script_refuses_a_version_that_never_arrived():
    """Отметка без читателя бесполезна — читатель здесь `update_windows.ps1`.

    Печатать мало: зелёный накат на старом коде выглядит как успешный, и строку
    «на диске лежит pm113» человек пролистает так же, как пролистал отсутствие
    «Running upgrade». Поэтому у скрипта есть `-Tag`: человек называет версию,
    которую СОБИРАЛСЯ поставить, и при расхождении накат отказывается работать —
    до копии базы и до миграций, то есть ничего не успев сделать.
    """
    text = (ROOT / "deploy" / "update_windows.ps1").read_text(encoding="utf-8-sig")

    assert "param([string]$Tag" in text, "нечем назвать ожидаемую версию"
    assert "INSTALLED_TAG" in text, "отметку никто не читает"
    guard = text[text.index("$installed = "):text.index("Зависимости")]
    assert "exit 1" in guard, "расхождение версий обязано останавливать накат"
    # До копии базы и миграций: остановиться надо раньше, чем что-то сделано.
    assert text.index("INSTALLED_TAG") < text.index("Копия базы перед миграцией")
