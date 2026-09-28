"""Регрессии второго аудита: тупики и потерянный ввод в диалогах и меню."""
import asyncio
from types import SimpleNamespace

from telegram.constants import ChatType

from conftest import CHAT, answer, payment, png
from finance.bot import build_app
from finance.flow import MENU_INPUT_TTL, MENU_RECORDS, Reply


def buttons(reply):
    return [data for row in reply.buttons for _, data in row]


def test_add_card_prompt_does_not_swallow_expense(env):
    """«Добавить карту», а потом расход текстом: карта не создаётся молча,
    а «Нет — это не карта» записывает расход."""
    db, rec, flow = env
    before = len(db.cards())
    [prompt] = flow.on_button(CHAT, "m:addcard")
    assert "m:no" in buttons(prompt)                          # можно передумать
    [confirm] = flow.on_text(CHAT, "3500 доставка СДЭК вчера")
    assert confirm.text.startswith("Добавить карту") and len(db.cards()) == before
    rec.payments.append(payment(amount="3500"))
    [saved] = flow.on_button(CHAT, "m:notit")
    assert saved.text.startswith("✅ Записано") and len(db.cards()) == before


def test_menu_input_expires(env):
    db, rec, flow = env
    now = [1000.0]
    flow.clock = lambda: now[0]
    flow.on_button(CHAT, "m:addcat")
    now[0] += MENU_INPUT_TTL + 1
    rec.payments.append(payment(amount="999"))
    [r] = flow.on_text(CHAT, "999 такси")                     # давно — это уже расход
    assert r.text.startswith("✅ Записано")


def test_bad_menu_input_keeps_waiting_and_cancel_returns_question(env):
    db, rec, flow = env
    rec.payments.append(payment(date=""))                    # вопрос о дате
    flow.on_files(CHAT, [png()], "")
    flow.on_button(CHAT, "m:addcat")
    [bad] = flow.on_text(CHAT, "05.09")                       # не статья
    assert "словами" in bad.text and "m:no" in buttons(bad)
    assert "05.09" not in [c["name"] for c in db.categories()]
    cancel = flow.on_button(CHAT, "m:no")
    assert "Когда была оплата" in cancel[-1].text             # вернулись к вопросу
    flow.on_text(CHAT, "05.09")
    assert db.expenses("2026-09")[0].op_date == "2026-09-05"


def test_draft_button_clears_menu_input(env):
    db, rec, flow = env
    rec.payments.append(payment(date=""))
    flow.on_files(CHAT, [png()], "")
    flow.on_button(CHAT, "m:addcat")
    answer(flow, "d:date:1")                                  # ответил на вопрос кнопкой
    rec.payments.append(payment(amount="700"))
    [r] = flow.on_text(CHAT, "700 такси")
    assert r.text.startswith("✅ Записано")


def test_statement_text_not_taken_as_draft_answer(env):
    db, rec, flow = env
    rec.payments.append(payment(currency="USD", amount="20"))
    flow.on_files(CHAT, [png()], "")                          # спрашивает сумму в рублях
    flow.on_button(CHAT, "s:m:2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    rec.statements.append({"total_in": "120000", "total_out": "95000", "operations": [],
                           "period_from": "2026-09-01", "period_to": "2026-09-30"})
    flow.on_text(CHAT, "пришло 120000, ушло 95000")
    assert [c[0] for c in rec.calls][-1] == "statement"       # ушло в выписку
    assert db.expenses("2026-09") == []                       # а не расходом


def test_stale_and_unknown_buttons_always_answer(env):
    db, rec, flow = env
    for data in ("m:delcard:999", "m:list:мусор", "t:zzz:1", "x:1", "e:zzz:1"):
        replies = flow.on_button(CHAT, data)
        assert replies and replies[-1].text, data


def test_deleted_card_in_old_picker(env):
    db, rec, flow = env
    rec.payments.append(payment())
    flow.on_files(CHAT, [png()], "")
    e = db.expenses("2026-09")[0]
    spare = db.add_card("Запасная", "9999", "Банк")
    [picker] = flow.on_button(CHAT, f"e:card:{e.id}")
    assert f"e:keep:{e.id}" in buttons(picker)
    current = [label for row in picker.buttons for label, _ in row if label.startswith("✓ ")]
    assert current and e.card in current[0]
    db.delete_card(spare.id)
    [again] = flow.on_button(CHAT, f"e:card:{e.id}:{spare.id}")   # не IntegrityError
    assert "уже нет" in again.text and again.buttons


def test_transfer_side_deleted_card(env):
    db, rec, flow = env
    rec.payments.append(payment(own_transfer=True, card_last4="1111", counterparty_last4="2222",
                                counterparty_bank="Т-Банк", merchant="", category=""))
    flow.on_files(CHAT, [png()], "")
    spare = db.add_card("Запасная", "9999", "Банк")
    db.delete_card(spare.id)
    [r] = flow.on_button(CHAT, f"t:to:1:{spare.id}")
    assert "уже нет" in r.text and "t:keep:1" in buttons(r)


def test_questions_have_skip(env):
    db, rec, flow = env
    rec.payments.append(payment(looks_personal=True))
    [q] = flow.on_files(CHAT, [png()], "")
    assert any(b.endswith(":skip") for b in buttons(q))
    [r] = answer(flow, "d:skip")
    assert "Не записываю" in r.text and db.expenses("2026-09") == []


def test_plural_files():
    from finance.flow import _plural
    assert [_plural(n, "файл", "файла", "файлов") for n in (1, 2, 5, 11, 21, 22, 112)] == \
        ["файл", "файла", "файлов", "файлов", "файл", "файла", "файлов"]


def test_addcat_says_added_or_exists(env):
    db, rec, flow = env
    assert "Добавил статью" in flow.on_command(CHAT, "addcat", "Новая")[0].text
    assert "уже есть" in flow.on_command(CHAT, "addcat", "Новая")[0].text


def test_reset_waits_for_running_request():
    """/reset в тестовом боте не рвёт уже идущую обработку: Flow берётся под замком."""
    flows = {"current": None}
    order = []

    class SlowFlow:
        def __init__(self, name):
            self.name = name

        def on_text(self, chat_id, text):
            order.append((self.name, text))
            return [Reply("ok")]

    flows["current"] = SlowFlow("old")

    def reset(uid):
        flows["current"] = SlowFlow("new")
    app = build_app("123:ABC", lambda uid: flows["current"], {1}, label="🧪", reset=reset)
    handlers = {type(h).__name__: h for h in app.handlers[0]}
    text = next(h for h in app.handlers[0] if type(h).__name__ == "MessageHandler"
                and "TEXT" in str(h.filters).upper())

    async def noop(*a, **kw):
        return None

    def upd(t=None, data=None):
        chat = SimpleNamespace(id=1, type=ChatType.PRIVATE, send_message=noop, send_action=noop,
                               send_document=noop)
        q = SimpleNamespace(data=data, answer=noop, edit_message_reply_markup=noop)
        return SimpleNamespace(effective_chat=chat, effective_user=SimpleNamespace(id=1),
                               message=SimpleNamespace(text=t), callback_query=q)

    async def scenario():
        await handlers["CallbackQueryHandler"].callback(upd(data="reset:yes"), None)
        await text.callback(upd("после сброса"), None)
    asyncio.run(scenario())
    assert order == [("new", "после сброса")]
    assert MENU_RECORDS  # меню не мешает


def test_expense_card_can_be_finished(env):
    """Карточка расхода: «Готово» закрывает её; выбор той же карты/статьи —
    «Без изменений», другой — «Исправлено» (раньше карточка просто
    возвращалась та же, и казалось, что бот застрял)."""
    db, rec, flow = env
    rec.payments.append(payment())
    [card] = flow.on_files(CHAT, [png()], "")
    e = db.expenses("2026-09")[0]
    assert f"e:ok:{e.id}" in buttons(card)
    [same] = flow.on_button(CHAT, f"e:card:{e.id}:{e.card_id}")
    assert same.text.startswith("Без изменений") and f"e:ok:{e.id}" in buttons(same)
    other = next(c for c in db.cards() if c.id != e.card_id)
    [changed] = flow.on_button(CHAT, f"e:card:{e.id}:{other.id}")
    assert changed.text.startswith("Исправлено: карта") and db.expense(e.id).card_id == other.id
    cat = db.category_id(e.category)
    [same_cat] = flow.on_button(CHAT, f"e:cat:{e.id}:{cat}")
    assert same_cat.text.startswith("Без изменений")
    [done] = flow.on_button(CHAT, f"e:ok:{e.id}")
    assert "сохранена" in done.text and not done.buttons and done.menu
