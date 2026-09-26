"""Два пользователя: у каждого своя база, каждый видит только свои данные."""
import asyncio
import os
import stat
import threading
import time
from types import SimpleNamespace

import pytest
from telegram.constants import ChatType

from conftest import TODAY, FakeRecognizer, payment, png
from finance.bot import build_app
from finance.storage import Storage
from finance.users import UserSpaces, parse_user_ids

ANNA, BORIS, STRANGER = 111, 222, 999


@pytest.fixture
def spaces(tmp_path):
    rec = FakeRecognizer()
    return UserSpaces(str(tmp_path / "data"), rec, [ANNA, BORIS], today=lambda: TODAY), rec


def test_each_user_sees_only_own_data(spaces):
    sp, rec = spaces
    anna, boris = sp.flow(ANNA), sp.flow(BORIS)
    anna.on_command(ANNA, "addcard", "Сбер 1111 Сбер")
    rec.payments.append(payment(merchant="Яндекс Директ", category="Реклама и продвижение",
                                category_confident=False))
    anna.on_files(ANNA, [png()], "")
    anna.on_button(ANNA, f"d:{anna.db.get_state(ANNA)['drafts'][0]['id']}:cat:"
                         f"{anna.db.category_id('Реклама и продвижение')}")
    assert len(anna.db.expenses("2026-09")) == 1 and anna.db.rules()

    # У Бориса — ничего из этого: ни карт, ни записей, ни правил, ни сводов.
    assert "Карт пока нет" in boris.on_command(BORIS, "cards")[0].text
    assert "записей нет" in boris.on_command(BORIS, "list", "2026-09")[0].text
    assert "Правил пока нет" in boris.on_command(BORIS, "rules")[0].text
    [itog] = boris.on_command(BORIS, "itog", "2026-09")
    assert "Яндекс" not in itog.text and "Ушло на бизнес: 0,00 ₽" in itog.text
    assert "Бизнес-расходов за этот период нет" in boris.on_command(BORIS, "svod", "2026-09")[0].text
    # Номер чужой записи ничего не даёт.
    assert "Укажите номер записи" in boris.on_command(BORIS, "fix", "1")[0].text
    assert "уже нет" in boris.on_button(BORIS, "e:del:1")[0].text
    assert len(anna.db.expenses("2026-09")) == 1


def test_separate_files_and_permissions(spaces):
    sp, _ = spaces
    sp.flow(ANNA), sp.flow(BORIS)
    a, b = sp.user_dir(ANNA), sp.user_dir(BORIS)
    assert a != b and os.path.exists(os.path.join(a, "finance.db"))
    assert os.path.exists(os.path.join(b, "finance.db"))
    assert stat.S_IMODE(os.stat(a).st_mode) == 0o700
    assert sp.flow(ANNA) is sp.flow(ANNA)             # одно соединение на пользователя


def test_same_screenshot_from_both_users_recorded_for_each(spaces):
    sp, rec = spaces
    shot = png()
    for uid in (ANNA, BORIS):
        fl = sp.flow(uid)
        fl.on_command(uid, "addcard", "Сбер 1111 Сбер")
        rec.payments.append(payment())
        [r] = fl.on_files(uid, [shot], "")
        assert r.text.startswith("✅ Записано №1")      # у каждого своя нумерация


def test_stranger_has_no_space(spaces):
    sp, _ = spaces
    with pytest.raises(PermissionError):
        sp.flow(STRANGER)
    assert not os.path.exists(sp.user_dir(STRANGER))


def test_parse_user_ids():
    assert parse_user_ids("111, 222 111") == [111, 222]
    with pytest.raises(ValueError):
        parse_user_ids("111,@anna")


def test_shared_database_moved_to_first_user(tmp_path):
    data = tmp_path / "data"
    (data / "receipts" / "2026-09").mkdir(parents=True)
    shot = data / "receipts" / "2026-09" / "a.png"
    shot.write_bytes(b"png")
    old = Storage(str(data / "finance.db"))
    card = old.add_card("Сбер", "1111", "Сбер")
    old.add_expense(op_date="2026-09-01", amount=500, card_id=card.id, receipt_path=str(shot))
    old.close()
    sp = UserSpaces(str(data), FakeRecognizer(), [ANNA, BORIS])
    assert sp.migrate_shared_data()
    assert not (data / "finance.db").exists()
    e = sp.flow(ANNA).db.expense(1)
    assert e.amount == 500 and os.path.exists(e.receipt_path)
    assert e.receipt_path.startswith(sp.user_dir(ANNA))
    assert sp.flow(BORIS).db.cards() == []
    assert sp.migrate_shared_data() is None            # повторно — ничего


# --- Telegram: маршрутизация по отправителю ---------------------------------

class UserFlow:
    def __init__(self, name, slow=0.0):
        self.name, self.slow, self.calls = name, slow, []

    def on_command(self, chat_id, command, arg):
        time.sleep(self.slow)
        self.calls.append(("command", chat_id, command))
        return []

    def on_button(self, chat_id, data):
        self.calls.append(("button", chat_id, data))
        return []

    def on_batch(self, chat_id, items):
        time.sleep(self.slow)
        self.calls.append(("batch", chat_id, len(items)))
        return []


def update(user_id, text="/list", data=None):
    async def noop(*a, **kw):
        return None
    chat = SimpleNamespace(id=user_id, type=ChatType.PRIVATE, send_message=noop,
                           send_action=noop, send_document=noop)
    query = SimpleNamespace(data=data, answer=noop, edit_message_reply_markup=noop)
    return SimpleNamespace(effective_chat=chat, effective_user=SimpleNamespace(id=user_id),
                           message=SimpleNamespace(text=text), callback_query=query)


def handlers(app):
    by_type = {}
    for h in app.handlers[0]:
        by_type.setdefault(type(h).__name__, h)
    return by_type


def test_messages_and_buttons_routed_by_sender():
    flows = {ANNA: UserFlow("anna"), BORIS: UserFlow("boris")}
    created = []

    def flow_for(uid):
        created.append(uid)
        return flows[uid]
    app = build_app("123:ABC", flow_for, {ANNA, BORIS})
    h = handlers(app)

    async def scenario():
        await h["CommandHandler"].callback(update(ANNA), SimpleNamespace(args=[]))
        await h["CallbackQueryHandler"].callback(update(BORIS, data="e:del:1"), None)
        await h["CommandHandler"].callback(update(STRANGER), SimpleNamespace(args=[]))
    asyncio.run(scenario())
    assert flows[ANNA].calls == [("command", ANNA, "list")]
    assert flows[BORIS].calls == [("button", BORIS, "e:del:1")]
    assert STRANGER not in created                      # чужому пространство не создаётся


def test_one_users_long_batch_does_not_block_other():
    flows = {ANNA: UserFlow("anna", slow=0.5), BORIS: UserFlow("boris")}
    app = build_app("123:ABC", flows.__getitem__, {ANNA, BORIS})
    h = handlers(app)
    finished = {}

    async def timed(uid, coro):
        await coro
        finished[uid] = time.monotonic()

    async def scenario():
        started = time.monotonic()
        await asyncio.gather(
            timed(ANNA, h["CommandHandler"].callback(update(ANNA), SimpleNamespace(args=[]))),
            timed(BORIS, h["CommandHandler"].callback(update(BORIS), SimpleNamespace(args=[]))))
        return started
    started = asyncio.run(scenario())
    assert finished[BORIS] - started < 0.3 < finished[ANNA] - started
