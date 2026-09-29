"""Коды поставки в txt — формат, как выдаёт СУЗ (ТЗ, 6.3).

Полный код на строку, настоящий GS (0x1D) внутри кода, каждая строка —
включая последнюю — заканчивается CRLF, без BOM. Побайтно как образец
`codes_12550.txt`.
Любая запись GS (`_x001D_` из xlsx, `\\u001d` из документации) приводится к
0x1D; короткий код (без криптохвоста) — отказ: по нему не напечатать этикетку
и не собрать файл поставки, а файл выдаётся как полный.
"""
from markapp.labels import FULL_RE, normalize


class CodesTxtError(ValueError):
    pass


def build(codes: list[str]) -> bytes:
    out, seen = [], set()
    for i, raw in enumerate(codes, 1):
        code = normalize(raw)
        if not FULL_RE.match(code):
            raise CodesTxtError(f"код №{i} не полный — в файл поставки идут только полные коды")
        if code in seen:
            raise CodesTxtError(f"код №{i} повторяется — один код не может стоять на двух вещах")
        seen.add(code)
        out.append(code)
    if not out:
        raise CodesTxtError("у поставки нет кодов")
    return "".join(c + "\r\n" for c in out).encode("ascii")


def filename(doc_number: str) -> str:
    return f"{doc_number}_коды.txt"
