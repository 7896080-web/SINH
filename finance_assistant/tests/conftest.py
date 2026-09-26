import threading
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
        # Для пачек (разбор параллельный, порядок вызовов не определён):
        # ответ по содержимому файла; значение-исключение — «ошибка распознавания».
        self.by_file: dict[bytes, object] = {}
        self._lock = threading.Lock()

    def _answer(self, files, queue):
        with self._lock:
            if files and files[0][0] in self.by_file:
                answer = self.by_file[files[0][0]]
            else:
                answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def recognize_payment(self, files, text, **kw):
        with self._lock:
            self.calls.append(("payment", files, text, kw))
        return self._answer(files, self.payments)

    def parse_statement(self, files, text, **kw):
        with self._lock:
            self.calls.append(("statement", files, text, kw))
        return self._answer(files, self.statements)


def payment(**over):
    base = {"is_payment": True, "direction": "out", "amount": "1500.00", "currency": "RUB",
            "date": "2026-09-20", "card_last4": "1111", "bank": "Сбер", "merchant": "СДЭК",
            "description": "доставка", "category": "Логистика и доставка",
            "category_confident": True, "looks_personal": False,
            "from_business_account": False, "own_transfer": False,
            "counterparty_last4": "", "counterparty_bank": ""}
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
_shots = iter(range(1, 10**6))


def png():
    """Новый «скриншот»: у каждого своё содержимое, как у настоящих."""
    return (b"\x89PNG fake %d" % next(_shots), "image/png")


def answer(flow, data: str, chat: int = CHAT):
    """Нажать кнопку текущего вопроса: как в Telegram, в неё зашит номер операции."""
    if data.startswith("d:"):
        draft_id = flow.db.get_state(chat)["drafts"][0]["id"]
        data = f"d:{draft_id}:{data[2:]}"
    return flow.on_button(chat, data)
