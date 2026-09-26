import io
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from conftest import CHAT
from finance.money import format_amount, parse_amount
from finance.recognize import ClaudeRecognizer, RecognitionError, file_blocks


@pytest.mark.parametrize("text, kopecks", [("1234,5", 123450), ("1 234.56", 123456), ("7", 700)])
def test_parse_amount(text, kopecks):
    assert parse_amount(text) == kopecks


def test_format_amount():
    assert format_amount(12345678) == "123 456,78"


def test_file_blocks():
    wb = Workbook()
    wb.active.append(["Дата", "Сумма"])
    wb.active.append(["01.09.2026", -1500])
    buf = io.BytesIO()
    wb.save(buf)
    blocks = file_blocks([
        (b"img", "image/png"), (b"%PDF", "application/pdf"),
        (buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("Дата;Сумма".encode("cp1251"), "text/csv"),
    ])
    assert [b["type"] for b in blocks] == ["image", "document", "text", "text"]
    assert "01.09.2026;-1500" in blocks[2]["text"]
    assert blocks[3]["text"] == "Дата;Сумма"


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.message


def fake_client(content, stop_reason="end_turn"):
    calls = []

    def stream(**kw):
        calls.append(kw)
        return FakeStream(SimpleNamespace(content=content, stop_reason=stop_reason))

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return client, calls


def text(t):
    return SimpleNamespace(type="text", text=t)


def test_recognizer_request_and_fallback_block():
    client, calls = fake_client([text('{"stale": 1}'), SimpleNamespace(type="fallback"),
                                 text('{"is_payment": true}')])
    r = ClaudeRecognizer(client=client)
    out = r.recognize_payment([(b"x", "image/png")], "такси", today="2026-09-26", cards=[],
                              categories=["Прочее"])
    assert out == {"is_payment": True}
    kw = calls[0]
    assert kw["model"] == "claude-opus-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["schema"]["properties"]["category"]["enum"] == ["Прочее", ""]
    assert kw["messages"][0]["content"][0]["type"] == "image"
    assert "такси" in kw["messages"][0]["content"][-1]["text"]


@pytest.mark.parametrize("content, stop", [([], "refusal"), ([text("{")], "max_tokens"),
                                           ([text("не json")], "end_turn")])
def test_recognizer_errors(content, stop):
    client, _ = fake_client(content, stop)
    with pytest.raises(RecognitionError):
        ClaudeRecognizer(client=client).parse_statement([], "итоги", period="сентябрь 2026",
                                                       cards=[], categories=["Прочее"])


def test_cards_commands(env):
    db, rec, flow = env
    assert "уже есть" in flow.on_command(CHAT, "addcard", "Сбер")[0].text
    assert "Добавил номера" in flow.on_command(CHAT, "addcard", "Сбер 9999")[0].text
    assert db.cards()[0].numbers == ["1111", "9999"]
    flow.on_command(CHAT, "addcard", "Озон 4444 Озон Банк")
    card = db.cards()[-1]
    assert (card.last4, card.bank) == ("4444", "Озон Банк")
    assert "Озон · 4444" in flow.on_command(CHAT, "cards")[0].text
    assert flow.on_command(CHAT, "delcard", str(card.id))[0].text == "Карта удалена."
    flow.on_command(CHAT, "addcat", "Обучение")
    assert "Обучение" in flow.on_command(CHAT, "cats")[0].text
    assert "Не знаю" in flow.on_command(CHAT, "foo")[0].text


def test_bot_module_builds():
    from finance.bot import _split, build_app
    assert [len(p) for p in _split("x" * 3000 + "\n" + "y" * 3000)] == [3000, 3000]
    app = build_app("123:ABC", flow=None, allowed={1})
    assert len(app.handlers[0]) == 5


def test_recognizer_fallback_continuation_kept():
    # Резервная модель продолжила начатый JSON — склеиваем, а не выбрасываем начало.
    client, _ = fake_client([text('{"is_payment": '), SimpleNamespace(type="fallback"),
                             text('true}')])
    out = ClaudeRecognizer(client=client).recognize_payment(
        [], "такси", today="2026-09-26", cards=[], categories=["Прочее"])
    assert out == {"is_payment": True}


def test_rub_spelled_out_is_rubles(env):
    from conftest import png, payment
    db, rec, flow = env
    rec.payments.append(payment(currency="руб."))
    [r] = flow.on_files(CHAT, [png()], "")
    assert r.text.startswith("✅")
