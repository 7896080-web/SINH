from datetime import date

import pytest

from finance.flow import Flow
from finance.storage import Storage

TODAY = date(2026, 9, 26)
CHAT = 1


class FakeRecognizer:
    """Отдаёт заранее заданные ответы вместо Claude и запоминает вызовы."""

    def __init__(self):
        self.payments: list[dict] = []
        self.statements: list[dict] = []
        self.calls: list[tuple] = []

    def recognize_payment(self, files, text, **kw):
        self.calls.append(("payment", files, text, kw))
        return self.payments.pop(0)

    def parse_statement(self, files, text, **kw):
        self.calls.append(("statement", files, text, kw))
        return self.statements.pop(0)


def payment(**over):
    base = {"is_payment": True, "direction": "out", "amount": "1500.00", "currency": "RUB",
            "date": "2026-09-20", "card_last4": "1111", "bank": "Сбер", "merchant": "СДЭК",
            "description": "доставка", "category": "Логистика и доставка",
            "category_confident": True, "looks_personal": False}
    base.update(over)
    return base


@pytest.fixture
def env(tmp_path):
    db = Storage(str(tmp_path / "f.db"))
    rec = FakeRecognizer()
    flow = Flow(db, rec, str(tmp_path / "receipts"), today=lambda: TODAY)
    db.add_card("Сбер", "1111", "Сбер")
    db.add_card("Тинькофф", "2222", "Т-Банк")
    db.add_card("Альфа", "3333", "Альфа-Банк")
    yield db, rec, flow
    db.close()


PNG = (b"\x89PNG fake", "image/png")
