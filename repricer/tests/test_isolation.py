"""«Репрайсер», sync_admin и «Маркировка» не импортируют друг друга.

Общий импорт связал бы их накаты: правка в `app/` ломала бы репрайсер, и
красный тест одной программы останавливал бы накат другой.
"""
import ast
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parent
FOREIGN = {".venv", "venv", "site-packages", "__pycache__", "backups", "logs"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def _own():
    for p in HERE.rglob("*.py"):
        if not FOREIGN & set(p.relative_to(HERE).parts):
            yield p


def test_repricer_imports_neither_sync_admin_nor_marking():
    bad = [str(p.relative_to(HERE)) for p in _own()
           if _imports(p) & {"app", "markapp", "upd_constructor", "lamoda_stickers"}]
    assert not bad, bad


def test_others_do_not_import_repricer():
    for d in ("app", "marking"):
        root = REPO / d
        if not root.exists():          # на офисном компьютере их рядом нет
            continue
        bad = [str(p.relative_to(REPO)) for p in root.rglob("*.py")
               if "__pycache__" not in p.parts and ".venv" not in p.parts and "priceapp" in _imports(p)]
        assert not bad, bad
