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
    # И НИ СЛОВА в stderr. Вывод наката человек читает ради двух строк — «DONE N»
    # и метки версии, — а первым, что он увидел 24.09, было предупреждение об
    # устаревшем `utcnow()` из этого же скрипта. Шум в начале вывода учит не
    # читать вывод целиком, а именно в нём живёт единственный признак того, что
    # накат прошёл не так.
    assert r.stderr.strip() == "", f"накат заговорил не по делу: {r.stderr[-500:]}"

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


def test_the_apply_script_does_not_call_a_deprecated_clock(tmp_path):
    """Сканер ИСХОДНИКА, а не поведения, — и по той же причине, что у
    `test_local_calendar_date.py`: увидеть это на машине разработки нельзя.

    `datetime.utcnow()` помечен устаревшим с Python 3.12, а здесь 3.11 — то есть
    предупреждения тут не будет никогда, сколько ни гоняй apply-скрипт. На бою
    Python новее, и 24.09 накат начался с четырёх строк DeprecationWarning,
    после которых шли «DONE 6» и метка версии — единственные две строки, ради
    которых вывод и читают. Шум в начале учит не дочитывать, а именно в выводе
    живёт признак того, что накат прошёл не так (23.09 им было ОТСУТСТВИЕ
    строки «Running upgrade»).

    Проверка стоит на тексте apply-скрипта: он уезжает на сервер целиком, и
    именно он там исполняется.
    """
    source = _apply_source(_generate(tmp_path, ["README.md"]))
    # Комментарии выкидываем: в самом скрипте про `utcnow()` написано словами —
    # зачем его тут нет, — и сканер, читающий текст наравне с кодом, падал бы на
    # собственном объяснении. Проверка про ВЫЗОВ, а не про упоминание.
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert "utcnow(" not in code, (
        "apply-скрипт зовёт `datetime.utcnow()` — на бою это DeprecationWarning "
        "первой строкой наката; берите `datetime.now(datetime.timezone.utc)`")


# ---------------------------------------------------------------------------
# То же правило — для скриптов, лежащих в `deploy/`
# ---------------------------------------------------------------------------

def test_every_russian_ps1_in_deploy_has_a_bom():
    """BOM обязателен КАЖДОМУ `.ps1` с кириллицей, а не только патчу.

    Правило записано и закрыто тестом для генератора, но сами скрипты `deploy/`
    под него не проверялись — и `enable_log_rotation.ps1` лежал без BOM. При
    запуске ФАЙЛОМ PowerShell 5.1 читает такой скрипт как cp1251: кириллица
    превращается в мусор, а длинное тире (три байта в UTF-8) разбирается как
    три случайных символа — и однажды один из них закроет строковый литерал
    раньше времени, уронив разбор до первой команды.

    Вставка в консоль это не ловит: там кодировка уже верная. То есть дефект
    виден только при том способе запуска, которым скрипты и запускают.
    """
    import io
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "deploy").glob("*.ps1")):
        raw = path.read_bytes()
        if not raw:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            offenders.append(f"{path.name}: не читается как UTF-8")
            continue
        has_cyrillic = any("А" <= ch <= "я" or ch == "ё" or ch == "Ё" for ch in text)
        if has_cyrillic and not raw.startswith(b"\xef\xbb\xbf"):
            offenders.append(f"{path.name}: кириллица без BOM")

    assert offenders == [], (
        "скрипты запускают файлом, а PowerShell 5.1 без BOM читает их как "
        f"cp1251: {offenders}")
