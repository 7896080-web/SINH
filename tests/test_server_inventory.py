"""ОПИСЬ: отвечает ли диск сервера на вопрос «это репозиторий?».

Сверка sha у файлов патча отвечает на другой вопрос — «доехало ли то, что я
послал», — и на этот не отвечает ПО ПОСТРОЕНИЮ: она сверяет то, что В патче, а
не то, чего в нём нет. 15.10 это остановило накат живого сервера:
`scripts/probe_accounts.py` не привозил ни один патч, на сервере его не было
вовсе, и узналось это из десяти упавших тестов через тридцать три минуты после
начала наката — уже после копии базы и миграций. `INSTALLED_TAG` при этом
сходился, sha сходились, то есть все три предохранителя честно отвечали «всё
хорошо».

Опись несёт sha КАЖДОГО файла релиза и потому отвечает сразу на три вопроса:
чего НЕТ, что РАЗОШЛОСЬ и что лежит ЛИШНЕГО (осадок прежних патчей — они ведь
только пишут и никогда не удаляют).

Проверяем по СЛЕДСТВИЮ: собираем патч настоящим генератором, распаковываем
нагрузку и ПРОГОНЯЕМ apply-скрипт с подменённым корнем — ровно так, как это
случится на сервере. Проверять генератор по исходнику мало: предмет правки —
то, что окажется на диске у человека.
"""
import base64
import os
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "scripts" / "gen_whole_file_patch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import gen_whole_file_patch as gen  # noqa: E402

# Признак «описи есть из чего собраться» — не каталог `.git`, а СОБРАЛАСЬ ЛИ
# она. Это тот же класс, что `1c/` (см. tests/test_repository_only_marks.py),
# только механизм другой и потому не пойманный прежним правилом: список файлов
# релиза берётся из `git ls-files`, а на боевом сервере git не установлен
# вовсе. `tracked_files` там возвращает пустой список НАМЕРЕННО — иначе патч
# перестал бы собираться, то есть новая диагностика отняла бы главное, —
# значит `inventory_of` пуст, «сервер» из описи не создаёт ни одного файла, и
# всё, что на нём стоит, падает ВСЕГДА. Здесь при этом зелено, то есть
# починить по месту нельзя: 29.10 на pm167 так встали пять тестов, накат
# остановился ПЕРЕД перезапуском служб, уже после копии базы и миграций.
#
# Спрашиваем ИМЕННО результат, а не наличие `.git`: каталог может лежать и там,
# где `git` не запускается (нет бинаря, чужой владелец, `safe.directory`), и
# тогда признак соврал бы в самую дорогую сторону — тесты пошли бы по пустой
# описи.
INVENTORY_BUILDS = bool(gen.tracked_files(str(ROOT)))
repository_only = pytest.mark.skipif(
    not INVENTORY_BUILDS,
    reason="опись собирается из `git ls-files`: на боевом сервере git не "
           "установлен, и список файлов релиза там пуст по построению",
)


def _child_env() -> dict:
    """Кодировка с ОБЕИХ сторон — см. tests/test_subprocess_encoding.py."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _apply_source(tmp_path, root=ROOT) -> str:
    """Собрать патч настоящим генератором и достать из него apply-скрипт."""
    out = tmp_path / "probe_deploy.ps1"
    done = subprocess.run(
        [sys.executable, str(GEN), "--tag", "probe", "--parts", "1",
         "--root", str(root), "--out", str(out), "CLAUDE.md"],
        cwd=str(ROOT), capture_output=True, encoding="utf-8",
        errors="replace", env=_child_env())
    assert done.returncode == 0, done.stdout + done.stderr
    _apply_source.last_stdout = done.stdout

    text = out.read_text(encoding="utf-8-sig")
    blocks = re.findall(r"@'\r?\n(.*?)\r?\n'@", text, re.S)
    b64 = "".join("".join(b.split()) for b in blocks)
    return zlib.decompress(base64.b64decode(b64)).decode("utf-8")


def _server(tmp_path) -> Path:
    """«Сервер»: на диске ровно то, что обещает опись, — как сразу после установки.

    Копируем ПО ОПИСИ, а не обходом каталогов: в рабочем дереве лежат и
    неотслеживаемые файлы (черновик, временная копия модуля от соседнего
    теста), и они честно попали бы в «лишние» — то есть проверка «на исправном
    диске опись молчит» падала бы от чужой работы рядом.
    """
    server = tmp_path / "server"
    for rel, _sha in gen.inventory_of(str(ROOT)):
        dst = server / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    return server


def _run_apply(tmp_path, server: Path, root=ROOT) -> str:
    script = tmp_path / "apply.py"
    script.write_text(
        _apply_source(tmp_path, root).replace('ROOT = r"C:\\sync_admin"',
                                              "ROOT = r" + repr(str(server))),
        encoding="utf-8")
    done = subprocess.run([sys.executable, str(script)], capture_output=True,
                          encoding="utf-8", errors="replace", env=_child_env())
    assert done.returncode == 0, done.stdout + done.stderr
    assert "DONE" in done.stdout, done.stdout
    return done.stdout


@repository_only
def test_the_inventory_names_what_is_missing_changed_and_extra(tmp_path):
    """Три вопроса, три ответа — и все три по именам, а не числом.

    `scripts/probe_accounts.py` этот патч НЕ ВЕЗЁТ: в том и смысл, что опись
    видит дальше собственной нагрузки. Именно этот файл 15.10 и пропал.
    """
    server = _server(tmp_path)
    os.remove(server / "scripts" / "probe_accounts.py")
    (server / "app" / "report.py").write_text("правил руками\n", encoding="utf-8")
    (server / "tests" / "test_stale_from_pm99.py").write_text("x = 1\n")

    out = _run_apply(tmp_path, server)
    written = (server / "deploy" / "INVENTORY.txt").read_text(encoding="utf-8-sig")

    for where in (out, written):
        assert "scripts/probe_accounts.py" in where, where[-800:]
        assert "app/report.py" in where, where[-800:]
        assert "tests/test_stale_from_pm99.py" in where, where[-800:]
    assert "нет на диске: 1" in written
    assert "разошлись с релизом: 1" in written
    assert "лишние, в релизе их нет: 1" in written


@repository_only
def test_a_disk_that_matches_the_release_says_so(tmp_path):
    """Обратная проверка, без которой первая молчала бы и на «ругаемся всегда».

    Опись, у которой каждый накат что-то «не сходится», пролистывается вместе с
    настоящей пропажей — ровно то, из-за чего отчёт о расхождениях в этом
    проекте обязан молчать на исправной системе.
    """
    server = _server(tmp_path)
    # Следы прежних накатов мешать не должны: их пишет сам же apply-скрипт, а
    # `.bak_` остаётся от каждой перезаписи.
    (server / "deploy" / "INSTALLED_TAG").write_text("pm1\n", encoding="utf-8")
    (server / "app" / "report.py.bak_pm99").write_text("старое\n", encoding="utf-8")
    (server / "tests" / "__pycache__").mkdir()
    (server / "tests" / "__pycache__" / "test_x.cpython-311.pyc").write_bytes(b"\x00")

    _run_apply(tmp_path, server)
    written = (server / "deploy" / "INVENTORY.txt").read_text(encoding="utf-8-sig")

    assert "нет на диске: 0" in written, written[:600]
    assert "разошлись с релизом: 0" in written, written[:600]
    assert "лишние, в релизе их нет: 0" in written, written[:600]


@repository_only
def test_a_broken_inventory_never_kills_the_patch(tmp_path):
    """Опись — диагностика, и ронять ею накат нельзя.

    Файлы к этому моменту уже записаны и сверены по sha; отказ ПОСЛЕ записи
    человек прочитает как «патч не встал» и пойдёт откатывать то, что встало
    верно. Но и глотать нельзя: причина обязана быть названа.
    """
    server = _server(tmp_path)
    # Записать опись не получится: на её месте каталог.
    (server / "deploy" / "INVENTORY.txt").mkdir()

    out = _run_apply(tmp_path, server)

    assert "опись не собралась" in out, out[-500:]
    assert "DONE" in out


@repository_only
def test_every_file_in_the_root_is_a_decision(tmp_path):
    """Новый файл в корне не попадёт в релиз МОЛЧА.

    Каталоги релиза берутся целиком, а корень — поимённо, и два списка обязаны
    покрывать его без остатка: иначе однажды в корне появится файл, от которого
    зависит работа, и опись про него не скажет ничего.
    """
    tracked = {rel for rel in gen.tracked_files(str(ROOT)) if "/" not in rel}
    decided = set(gen.RELEASE_ROOT_FILES) | set(gen.NOT_SHIPPED_ROOT)

    assert tracked == decided, (
        "решите про каждый: везём (RELEASE_ROOT_FILES) или нет "
        "(NOT_SHIPPED_ROOT). Не решено: " + repr(sorted(tracked ^ decided)))


@repository_only
def test_the_inventory_covers_the_tools_and_skips_what_we_never_ship():
    """Состав описи: скрипты разбора внутри, `1c/` снаружи.

    Скрипты — то, за чем идут, когда уже беда, и их отсутствие замечают в
    худший час. А каталог `1c/` накат не везёт намеренно, и требовать его с
    сервера значило бы кричать на исправной системе каждый раз.
    """
    inside = {rel for rel, _sha in gen.inventory_of(str(ROOT))}

    assert "scripts/probe_accounts.py" in inside
    assert "scripts/restore_db.py" in inside
    assert "deploy/update_windows.ps1" in inside
    assert "alembic.ini" in inside
    assert not [rel for rel in inside if rel.startswith("1c/")]
    assert not [rel for rel in inside if rel.endswith("АУДИТ.md")]


def test_the_inventory_has_a_reader_in_the_update_script():
    """Опись без читателя бесполезна ровно так же, как находка без читателя.

    Читает её `update_windows.ps1`, и читает ДО копии базы и миграций: человек
    смотрит в это место, потому что здесь же называется версия.
    """
    text = (ROOT / "deploy" / "update_windows.ps1").read_text(encoding="utf-8-sig")

    assert "INVENTORY.txt" in text, "опись никто не читает"
    assert text.index("INVENTORY.txt") < text.index("Копия базы перед миграцией")
    assert "нет на диске" in text, "счётчики опознаются поимённо"


@repository_only
def test_a_patch_without_an_inventory_builds_and_says_so(tmp_path):
    """Опись не имеет права отнять у патча возможность привезти файлы.

    Корень без git — обычное дело: так патч собирают проверки с подменённым
    корнем. Пока опись собиралась через `check=True`, такая сборка ПАДАЛА
    целиком, то есть новая диагностика отнимала главное — доставку на боевой
    сервер. Поймано соседним тестом, а не рассуждением.

    И вторая сторона, которая дороже: пустая опись на сервере НЕ означает «на
    диске лишнее всё». Без списка известных файлов в «лишние» ушёл бы весь
    релиз — триста с лишним имён тревоги на исправной установке, после которых
    опись не читают больше никогда.
    """
    fake = tmp_path / "norepo"
    (fake / "deploy").mkdir(parents=True)
    shutil.copy2(ROOT / "CLAUDE.md", fake / "CLAUDE.md")

    server = _server(tmp_path)
    out = _run_apply(tmp_path, server, root=fake)

    assert "ОПИСЬ НЕ СОБРАНА" in _apply_source.last_stdout, (
        "патч без описи обязан сказать это вслух при сборке")
    assert "опись не собралась" in out, out[-400:]
    assert "лишний" not in out, "пустая опись объявила лишним весь релиз"
    assert "DONE" in out


def test_reading_the_inventory_can_never_abort_the_update():
    """Чтение описи — под `try`, и это не перестраховка.

    В `update_windows.ps1` стоит `$ErrorActionPreference = "Stop"`, то есть
    ЛЮБАЯ заминка (файла нет, на его месте каталог, не хватило прав) оборвала
    бы накат — и оборвала бы ровно там, где новые файлы на диске уже лежат, а
    службы работают ещё на старом коде. Диагностика не имеет права ронять то,
    что она описывает; PowerShell тут не запустить, поэтому проверяем
    исходником.
    """
    text = (ROOT / "deploy" / "update_windows.ps1").read_text(encoding="utf-8-sig")
    assert '$ErrorActionPreference = "Stop"' in text, (
        "если это убрали, проверка ниже сторожит пустоту")

    head = text[:text.index("$invPath")]
    tail = text[text.index("$invPath"):text.index("Зависимости")]

    assert head.rstrip().endswith("try {"), "чтение описи не обёрнуто в try"
    assert "} catch {" in tail, "у try нет catch — накат оборвётся"
