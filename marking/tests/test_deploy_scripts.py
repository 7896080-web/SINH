"""Скрипты `.ps1` с кириллицей обязаны быть с BOM (правило sync_admin).

PowerShell 5.1 читает файл без BOM как cp1251: кириллица превращается в мусор,
а длинное тире разбирается как три символа и может закрыть строковый литерал
раньше времени — скрипт падает до первой команды. Вставка в консоль этого не
ловит, поэтому ловит тест.
"""
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def test_every_russian_ps1_has_a_bom():
    scripts = list(DEPLOY.glob("*.ps1"))
    assert scripts
    for p in scripts:
        raw = p.read_bytes()
        text = raw.decode("utf-8-sig")
        if any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text):
            assert raw.startswith(b"\xef\xbb\xbf"), p.name


def test_scripts_never_touch_sync_admin_services():
    for p in DEPLOY.glob("*.ps1"):
        text = p.read_text(encoding="utf-8-sig")
        for svc in ("sync_admin_web", "sync_admin_worker"):
            assert f"nssm restart {svc}" not in text and f"nssm stop {svc}" not in text, p.name
