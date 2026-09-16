"""Хэши файлов «по коду»: для .py — AST без docstring и комментариев (ast.unparse),
для остальных — по байтам. Нужно, чтобы сравнивать минифицированные серверные файлы
с релизом: DIFF здесь означает реальное различие в логике, а не в оформлении.

    python scripts/normalized_hashes.py            # печатает dict {path: sha}
"""
import ast, hashlib, io, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = ["app", "alembic/versions"]


def _strip_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def norm_hash(path):
    b = io.open(path, "rb").read()
    if path.endswith(".py"):
        try:
            src = ast.unparse(_strip_docstrings(ast.parse(b.decode("utf-8"))))
            return hashlib.sha256(src.encode("utf-8")).hexdigest()
        except SyntaxError:
            return "SYNTAX_ERROR"
    return hashlib.sha256(b).hexdigest()


def collect(root):
    out = {}
    for d in DIRS:
        for dp, dn, fn in os.walk(os.path.join(root, d)):
            dn[:] = [x for x in dn if x != "__pycache__"]
            for f in fn:
                if f.endswith((".py", ".html")):
                    p = os.path.join(dp, f)
                    out[os.path.relpath(p, root).replace(os.sep, "/")] = norm_hash(p)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(collect(ROOT), indent=1, sort_keys=True))
