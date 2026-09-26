"""Массовая отправка скриншотов: параллельный разбор, одна сводка, вопросы по одному."""
import asyncio
import time
from types import SimpleNamespace

from telegram.constants import ChatType

from conftest import CHAT, TODAY, answer, payment, png
from finance.bot import build_app
from finance.recognize import RecognitionError
from test_sverka import op, statement


def item(shot, caption=""):
    return dict(files=[shot], caption=caption, filename="", file_ids=None)


def test_batch_summary_and_questions(env):
    db, rec, flow = env
    # Раньше уже записанный скриншот — в пачке он будет повтором.
    old = png()
    rec.payments.append(payment(amount="999", merchant="Старый"))
    flow.on_files(CHAT, [old], "")

    shots = [png() for _ in range(6)]
    rec.by_file.update({
        shots[0][0]: payment(amount="1000", merchant="СДЭК"),
        shots[1][0]: payment(amount="2000", merchant="Озон", category="Маркетплейсы: комиссии и услуги"),
        shots[2][0]: payment(amount="3000", merchant="Неизвестно", card_last4="", bank=""),
        shots[3][0]: {**payment(), "is_payment": False},
        shots[4][0]: payment(amount="4000", merchant="Контур", category="Связь, сервисы, подписки"),
        shots[5][0]: RecognitionError("сервис распознавания перегружен"),
    })
    replies = flow.on_batch(CHAT, [item(s) for s in shots] + [item(shots[0]), item(old)])
    head, summary, question = replies
    assert head.text.startswith("📥 Разобрал 8 файлов.")
    assert "🔁 Повторы (уже записаны или в этой же пачке): 2" in head.text
    assert "🚫 Не похоже на оплату: 1" in head.text
    assert "⚠️ Не распознано: 1 (сервис распознавания перегружен)" in head.text
    # Три записались сразу, одной сводкой с номерами.
    assert summary.text.startswith("✅ Записано как бизнес: 3 на 7 000,00 ₽")
    assert "№2 20.09  1 000,00 ₽  Сбер" in summary.text and "/fix" in summary.text
    # По четвёртой — вопрос о карте, с номером операции в кнопках.
    assert "С какой карты" in question.text
    assert len(rec.calls) == 1 + 6   # повторы в Claude не отправлялись
    answer(flow, f"d:card:{db.cards()[1].id}")
    assert len(db.expenses("2026-09")) == 5
    assert "drafts" not in db.get_state(CHAT)


def test_batch_several_questions_numbered(env):
    db, rec, flow = env
    shots = [png() for _ in range(3)]
    for s in shots:
        rec.by_file[s[0]] = payment(amount=str(len(rec.by_file) + 1) + "00", card_last4="", bank="")
    replies = flow.on_batch(CHAT, [item(s) for s in shots])
    assert replies[-1].text.startswith("❓ Вопрос 1 из 3")
    replies = answer(flow, f"d:card:{db.cards()[0].id}")
    assert replies[-1].text.startswith("❓ Вопрос 1 из 2")   # осталось два
    answer(flow, f"d:card:{db.cards()[0].id}")
    replies = answer(flow, f"d:card:{db.cards()[0].id}")
    assert replies[-1].text.startswith("✅ Записано как бизнес: 1")
    assert len(db.expenses("2026-09")) == 3


def test_batch_transfers_income_and_rules(env):
    db, rec, flow = env
    shots = [png() for _ in range(3)]
    rec.by_file.update({
        shots[0][0]: payment(amount="100000", own_transfer=True, counterparty_last4="2222",
                             counterparty_bank="Т-Банк"),
        shots[1][0]: payment(direction="in", amount="500"),
        shots[2][0]: payment(merchant="Студия Вата", category_confident=False),
    })
    head, summary, question = flow.on_batch(CHAT, [item(s) for s in shots])
    assert "🔁 Переводы между своими счетами: 1" in summary.text
    assert "П1 20.09  100 000,00 ₽  Сбер → Тинькофф" in summary.text
    assert "↩️ Не записано: 1" in summary.text and "поступление" in summary.text
    answer(flow, f"d:cat:{db.category_id('Подрядчики и зарплата')}")
    # Статья, выбранная в пачке, запоминается для получателя.
    assert [r["merchant"] for r in db.rules()] == ["Студия Вата"]


def test_batch_recognition_runs_in_parallel(env):
    db, rec, flow = env
    shots = [png() for _ in range(8)]
    for s in shots:
        rec.by_file[s[0]] = payment(merchant=f"М{len(rec.by_file)}", amount=str(100 + len(rec.by_file)))
    original = rec.recognize_payment

    def slow(*a, **kw):
        time.sleep(0.2)
        return original(*a, **kw)
    rec.recognize_payment = slow
    started = time.monotonic()
    flow.on_batch(CHAT, [item(s) for s in shots])
    assert time.monotonic() - started < 1.0          # последовательно было бы ≥ 1,6 с
    # Порядок записей — как у файлов в пачке, несмотря на параллельный разбор.
    assert [e.merchant for e in db.expenses("2026-09")] == [f"М{i}" for i in range(8)]


def test_single_file_batch_behaves_like_before(env):
    db, rec, flow = env
    rec.payments.append(payment())
    [saved] = flow.on_batch(CHAT, [item(png())])
    assert saved.text.startswith("✅ Записано №")


def test_statement_batch_parallel_one_summary(env):
    db, rec, flow = env
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    pages = [(b"%%PDF page %d" % i, "application/pdf") for i in range(3)]
    rec.by_file.update({
        pages[0][0]: statement([op("2026-09-01", "100"), op("2026-09-02", "200")]),
        pages[1][0]: statement([op("2026-09-03", "300")]),
        pages[2][0]: statement([op("2026-09-04", "400")], last4="9999"),
    })
    [r] = flow.on_batch(CHAT, [item(p) for p in pages])
    assert r.text.startswith("📥 Пачка выписки по карте Сбер ·1111: 3 файлов")
    assert "• 1: Принято операций: 2" in r.text and "• 3: Принято операций: 1" in r.text
    assert "⚠️ В документе карта …9999" in r.text
    assert len(db.statement(db.cards()[0].id, "2026-09")[1]) == 4
    calls = len(rec.calls)
    [again] = flow.on_batch(CHAT, [item(pages[0]), item(pages[1])])
    assert "🔁 Уже загружены раньше: 2" in again.text and len(rec.calls) == calls


# --- Telegram: сборка альбома в пачку -------------------------------------

class FakeFlow:
    def __init__(self):
        self.calls = []

    def on_batch(self, chat_id, items):
        self.calls.append(("batch", len(items)))
        return []

    def on_command(self, chat_id, command, arg):
        self.calls.append(("command", command))
        return []


def fake_photo_update(n):
    async def noop(*a, **kw):
        return None

    async def get_file():
        async def download():
            return bytearray(b"photo %d" % n)
        return SimpleNamespace(download_as_bytearray=download)
    chat = SimpleNamespace(id=CHAT, type=ChatType.PRIVATE, send_message=noop, send_action=noop,
                           send_document=noop)
    photo = SimpleNamespace(file_unique_id=f"u{n}", get_file=get_file)
    message = SimpleNamespace(photo=[photo], caption=None, text="/list")
    return SimpleNamespace(effective_chat=chat, effective_user=SimpleNamespace(id=1),
                           message=message)


def test_album_collected_into_one_batch_and_command_waits(monkeypatch):
    import finance.bot as bot
    monkeypatch.setattr(bot, "BATCH_WAIT", 0.05)
    flow = FakeFlow()
    app = build_app("123:ABC", flow, {1})
    handlers = {type(h).__name__ + str(i): h for i, h in enumerate(app.handlers[0])}
    photo_h = next(h for h in handlers.values() if "PHOTO" in str(h.filters).upper())
    command_h = next(h for n, h in handlers.items() if n.startswith("CommandHandler"))

    async def scenario():
        for n in range(3):
            await photo_h.callback(fake_photo_update(n), None)
        await asyncio.sleep(0.2)                       # тишина — пачка ушла
        for n in range(3, 5):
            await photo_h.callback(fake_photo_update(n), None)
        # Команда сразу после файлов: сначала разбирается пачка, потом команда.
        await command_h.callback(fake_photo_update(9), SimpleNamespace(args=[]))
    asyncio.run(scenario())
    assert flow.calls == [("batch", 3), ("batch", 2), ("command", "list")]
