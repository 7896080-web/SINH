"""check_workstation.ps1 обязан показывать прокси и внешний адрес.

02.10.2026 вход в ЧЗ висел ReadTimeout из-за включённого VPN: по таймауту
причину не видно, а SFTP до сервера с чужого адреса не пускает правило
брандмауэра. Проверка компьютера должна называть обе вещи сама.
"""
from pathlib import Path

TEXT = (Path(__file__).resolve().parent.parent / "deploy" / "check_workstation.ps1").read_text(encoding="utf-8-sig")


def test_system_proxy_is_reported():
    assert "Internet Settings" in TEXT and "ProxyEnable" in TEXT and "AutoConfigURL" in TEXT
    assert "HTTPS_PROXY" in TEXT


def test_external_address_is_compared_with_the_office():
    assert '[string]$OfficeIp = "178.34.159.213"' in TEXT
    assert "api.ipify.org" in TEXT and "-eq $OfficeIp" in TEXT


def test_proxy_check_comes_before_the_crpt_probes():
    assert TEXT.index("ProxyEnable") < TEXT.index('Probe "True API auth/key"')
