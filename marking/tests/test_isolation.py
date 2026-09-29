"""Программа маркировки и sync_admin не импортируют друг друга (ТЗ, разд. 2).

Общий импорт связал бы их накаты: правка в `app/` ломала бы маркировку, и
красный тест одной программы останавливал бы накат другой.
"""
import ast
from pathlib import Path

MARKING = Path(__file__).resolve().parents[1]
REPO = MARKING.parent


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_marking_does_not_import_sync_admin():
    bad = [str(p.relative_to(MARKING)) for p in MARKING.rglob("*.py")
           if "app" in _imports(p)]
    assert not bad, f"импорт sync_admin из маркировки: {bad}"


def test_sync_admin_does_not_import_marking():
    app_dir = REPO / "app"
    if not app_dir.exists():      # на сервере маркировки кода sync_admin рядом нет
        return
    names = {"markapp", "upd_constructor", "lamoda_stickers"}
    bad = [str(p.relative_to(REPO)) for p in app_dir.rglob("*.py") if _imports(p) & names]
    assert not bad, f"импорт маркировки из sync_admin: {bad}"
