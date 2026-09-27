"""Регрессии второго аудита: безопасность страницы настроек, .env, файлы."""
import io
import logging
import socket
import threading
import zipfile

import pytest

from finance import mode, recognize
from finance.logfilter import SecretFilter, mask
from finance.settings_web import Guard, SettingsServer, hash_password
from finance.setup_web import format_errors, read_env, write_env



def test_env_value_with_newline_rejected(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n", encoding="utf-8")
    for bad in ("x\nLD_PRELOAD=/evil.so", "x B=1", "x\rB=1", "x\x85B=1"):
        with pytest.raises(ValueError):
            write_env(str(p), {"A": bad})
    assert read_env(str(p)) == {"A": "1"}


def test_token_and_key_format_checked():
    assert format_errors({"TELEGRAM_BOT_TOKEN": "123:abc_DEF-9"}) == []
    assert format_errors({"TELEGRAM_BOT_TOKEN": "123:abc\nX=1"})
    assert format_errors({"TELEGRAM_BOT_TOKEN_TEST": "нет"})
    assert format_errors({"ANTHROPIC_API_KEY": "sk-ant-ok_1"}) == []
    assert format_errors({"ANTHROPIC_API_KEY": "sk-ant-x y"})


def _raw_request(port, head: bytes):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(head)
    data = s.recv(200)
    s.close()
    return data


def test_bad_content_length_refused_without_reading(tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    srv = SettingsServer(("127.0.0.1", 0), str(env), hash_password("Пароль-длинный-1", n=2 ** 12),
                         checks=False, guard=Guard(), secure_cookie=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        for cl in (b"-1", b"abc", b"999999999"):
            reply = _raw_request(port, b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: "
                                 + cl + b"\r\n\r\n")
            assert reply.split()[1] in (b"400", b"413"), cl
    finally:
        srv.shutdown()
        srv.server_close()


def test_parallel_logins_limited():
    g = Guard()
    assert g.begin_attempt("1.1.1.1") is None
    assert g.begin_attempt("1.1.1.1")                 # тот же адрес, пока идёт проверка
    assert g.begin_attempt("2.2.2.2") is None
    assert g.begin_attempt("3.3.3.3")                 # не больше двух проверок разом
    g.end_attempt("1.1.1.1", ok=False)
    g.end_attempt("2.2.2.2", ok=True)
    for _ in range(4):                                # 1 + 4 = 5 неверных → блокировка
        assert g.begin_attempt("1.1.1.1") is None
        g.end_attempt("1.1.1.1", ok=False)
    assert "15 минут" in g.begin_attempt("1.1.1.1")


def test_trusted_ip_not_locked_out_by_global_limit():
    g = Guard()
    assert g.begin_attempt("10.0.0.1") is None
    g.end_attempt("10.0.0.1", ok=True)                # владелец однажды вошёл
    for i in range(30):                               # кто-то перебирает с разных адресов
        if g.begin_attempt(f"66.0.0.{i}") is None:
            g.end_attempt(f"66.0.0.{i}", ok=False)
    assert "на час" in (g.blocked("77.0.0.1") or "")
    assert g.blocked("10.0.0.1") is None


@pytest.mark.parametrize("prod,test", [("/", "/tmp/x"), ("/srv/Data", "/srv/data/..//Data/u")])
def test_overlap_edge_cases(prod, test):
    assert mode._overlap(test, prod)


def test_xlsx_bomb_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", "A" * (60 * 1024 * 1024))
    with pytest.raises(recognize.RecognitionError):
        recognize.xlsx_to_csv_text(buf.getvalue())
    with pytest.raises(recognize.RecognitionError):
        recognize.file_blocks([(b"x;" * 400_000, "text/csv")])


def test_secrets_masked_in_logs():
    token = "123456789:AAH" + "x" * 32
    assert token not in mask(f"https://api.telegram.org/bot{token}/getMe")
    assert "sk-ant-api03-" not in mask("key=sk-ant-api03-abcdefghijk")
    record = logging.LogRecord("t", logging.ERROR, "f", 1, "url %s", (f"bot{token}",), None)
    SecretFilter().filter(record)
    assert token not in record.getMessage()
