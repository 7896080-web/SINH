"""Порт и запуск «Ввода в оборот» рядом с «Маркировкой» и «Репрайсером».

08.10.2026: «Ввод в оборот» стоял на 8002, порт держал «Репрайсер», ярлык
полминуты ждал и писал «не запустилась» без причины. Обновление вдобавок
убивало ЛЮБОЙ процесс на своём порту.
"""
from pathlib import Path

from kizapp import config

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
TAKEN = {8001: "Маркировка", 8002: "Репрайсер"}


def _ps(name):
    return (DEPLOY / name).read_text(encoding="utf-8-sig")


def test_port_does_not_collide_with_neighbours():
    assert config.PORT not in TAKEN
    for name in ("install.ps1", "run.ps1", "update.ps1"):
        assert f"param([int]$Port = {config.PORT})" in _ps(name), name


def test_health_names_the_program(client):
    assert client.get("/health").json() == {"ok": True, "app": "kiz_intro"}


def test_launch_checks_identity_and_names_a_foreign_owner():
    run = _ps("run.ps1")
    assert '"app"\\s*:\\s*"kiz_intro"' in run
    assert "Get-NetTCPConnection" in run and "занят другой программой" in run


def test_update_stops_only_its_own_server():
    upd = _ps("update.ps1")
    block = upd[upd.index("Останавливаю программу"):upd.index("$db = ")]
    assert "OwningProcess" not in block, "убивать «кто держит порт» нельзя — там соседние программы"
    assert '*kizapp.web:app*' in block


def test_update_moves_port_in_env_and_shortcut():
    upd = _ps("update.ps1")
    assert "KIZ_PORT=$Port" in upd and "-replace '-Port \\d+'" in upd
