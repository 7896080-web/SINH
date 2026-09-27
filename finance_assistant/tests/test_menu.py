"""Кнопочное меню: главное меню внизу экрана и подменю под сообщениями."""
import asyncio
from types import SimpleNamespace

from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ChatType

from conftest import CHAT, png, payment
from finance.bot import RESET_LABEL, build_app, menu_keyboard
from finance.flow import (MENU_CARDS, MENU_ITOG, MENU_MORE, MENU_RECORDS, MENU_ROWS, MENU_SVERKA,
                          MENU_SVOD, Reply)


def buttons(reply):
    return [data for row in reply.buttons for _, data in row]


def labels(reply):
    return [text for row in reply.buttons for text, _ in row]


def test_every_main_button_answers(env):
    _, _, flow = env
    for label in (MENU_RECORDS, MENU_ITOG, MENU_SVERKA, MENU_SVOD, MENU_CARDS, MENU_MORE):
        replies = flow.on_text(CHAT, label)
        assert replies and replies[-1].text, label
        assert "Пришлите скриншот" not in replies[-1].text   # не принято за расход


def test_records_navigate_months(env):
    _, rec, flow = env
    rec.payments.append(payment(date="2026-08-20", amount="700"))
    flow.on_files(CHAT, [png()], "")
    [now] = flow.on_text(CHAT, MENU_RECORDS)
    assert "сентябрь" in now.text and buttons(now) == ["m:list:2026-08"]   # в будущее нельзя
    [aug] = flow.on_button(CHAT, "m:list:2026-08")
    assert "700" in aug.text and buttons(aug) == ["m:list:2026-07", "m:list:2026-09"]
    [jan] = flow.on_button(CHAT, "m:list:2026-01")
    assert buttons(jan) == ["m:list:2025-12", "m:list:2026-02"]


def test_itog_period_buttons_all_work(env):
    _, _, flow = env
    [ask] = flow.on_text(CHAT, MENU_ITOG)
    assert buttons(ask) == ["m:itog:2026-09", "m:itog:2026-08", "m:itog:2026-01..2026-09",
                            "m:itog:2025"]
    for data in buttons(ask):
        replies = flow.on_button(CHAT, data)
        assert replies[-1].file, data                       # отчёт с Excel


def test_sverka_button_mid_statement_offers_finish(env):
    db, _, flow = env
    [ask] = flow.on_text(CHAT, MENU_SVERKA)
    assert "s:m:2026-09" in buttons(ask)
    flow.on_button(CHAT, "s:m:2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    [mid] = flow.on_text(CHAT, MENU_SVERKA)                 # не принято за текст выписки
    assert buttons(mid) == ["m:done", "m:sverka"]
    flow.on_button(CHAT, "m:done")
    assert "statement" not in db.get_state(CHAT)            # загрузка закончена
    assert buttons(flow.on_text(CHAT, MENU_SVERKA)[-1])[0].startswith("s:m:")


def test_add_card_and_category_by_buttons(env):
    db, _, flow = env
    [cards] = flow.on_text(CHAT, MENU_CARDS)
    assert buttons(cards) == ["m:addcard", "m:delpick"]
    [ask] = flow.on_button(CHAT, "m:addcard")
    assert "Сбер 1234" in ask.text
    [confirm] = flow.on_text(CHAT, "ВТБ 4321 ВТБ")
    assert "Добавить карту: 💳 ВТБ · 4321 (ВТБ)?" == confirm.text
    flow.on_button(CHAT, "m:yes")
    assert any(c.name == "ВТБ" and "4321" in c.numbers for c in db.cards())
    # Следующее сообщение — снова обычное (как расход), а не карта.
    assert "Пришлите скриншот" in flow.on_text(CHAT, "привет")[-1].text

    [more] = flow.on_text(CHAT, MENU_MORE)
    assert "m:addcat" in buttons(more)
    flow.on_button(CHAT, "m:addcat")
    flow.on_text(CHAT, "Реклама в Telegram")
    flow.on_button(CHAT, "m:yes")
    assert "Реклама в Telegram" in [c["name"] for c in db.categories()]


def test_pending_input_dropped_by_other_actions(env):
    db, rec, flow = env
    before = len(db.cards())
    flow.on_button(CHAT, "m:addcard")
    flow.on_command(CHAT, "list")                            # передумал — команда
    rec.payments.append(payment(amount="999"))
    flow.on_text(CHAT, "999 такси")                          # это расход, не карта
    assert len(db.cards()) == before
    flow.on_button(CHAT, "m:addcat")
    flow.on_text(CHAT, MENU_RECORDS)                         # кнопка меню
    assert "Пришлите скриншот" in flow.on_text(CHAT, "Реклама")[-1].text


def test_delete_card_asks_confirmation(env):
    db, _, flow = env
    card = db.cards()[-1]
    [pick] = flow.on_button(CHAT, "m:delpick")
    assert f"m:delcard:{card.id}" in buttons(pick)
    [confirm] = flow.on_button(CHAT, f"m:delcard:{card.id}")
    assert buttons(confirm) == [f"m:delok:{card.id}", "m:no"]
    assert db.card(card.id)                                  # ещё не удалена
    flow.on_button(CHAT, "m:no")
    assert db.card(card.id)
    flow.on_button(CHAT, f"m:delok:{card.id}")
    assert db.card(card.id) is None


def test_card_with_records_is_not_deleted(env):
    db, rec, flow = env
    rec.payments.append(payment(card_last4="1111"))
    flow.on_files(CHAT, [png()], "")
    sber = next(c for c in db.cards() if "1111" in c.numbers)
    [answer] = flow.on_button(CHAT, f"m:delok:{sber.id}")
    assert "удалить нельзя" in answer.text and db.card(sber.id)


def test_more_menu_buttons(env):
    _, _, flow = env
    [more] = flow.on_text(CHAT, MENU_MORE)
    for data in buttons(more):
        if data in ("m:addcat",):
            continue
        assert flow.on_button(CHAT, data), data
    [stale] = flow.on_button(CHAT, "m:непонятно")          # старая/чужая кнопка — не тишина
    assert "неактуальна" in stale.text and stale.menu


def test_menu_button_works_while_question_pending(env):
    _, rec, flow = env
    db = flow.db
    rec.payments.append(payment(amount=""))                  # сумму не разобрал — спросит
    flow.on_files(CHAT, [png()], "")
    assert db.get_state(CHAT)["ask"] == "amount"
    replies = flow.on_text(CHAT, MENU_RECORDS)
    assert "записей нет" in replies[-1].text                 # меню, а не «сумма: 📋 Записи»
    assert db.get_state(CHAT)["ask"] == "amount"             # вопрос никуда не делся


# --- Telegram ---------------------------------------------------------------

class Flow:
    def on_command(self, chat_id, command, arg):
        return [Reply("Помощь", menu=True)] if command == "help" else [Reply("ок")]

    def on_text(self, chat_id, text):
        return [Reply("Выберите", [[("A", "x:1")]]), Reply("текст")]


def _update(user, text):
    sent = []

    async def send_message(text, **kw):
        sent.append((text, kw.get("reply_markup")))

    async def noop(*a, **kw):
        return None
    chat = SimpleNamespace(id=user, type=ChatType.PRIVATE, send_message=send_message,
                           send_action=noop, send_document=noop)
    return SimpleNamespace(effective_chat=chat, effective_user=SimpleNamespace(id=user),
                           message=SimpleNamespace(text=text)), sent


def _handler(app, name):
    return next(h for h in app.handlers[0] if type(h).__name__ == name
                and (name != "MessageHandler" or "TEXT" in str(h.filters).upper()))


def test_menu_keyboard_layout():
    kb = menu_keyboard()
    assert [[b.text for b in row] for row in kb.keyboard] == MENU_ROWS
    assert kb.resize_keyboard and kb.is_persistent
    assert [b.text for b in menu_keyboard(test=True).keyboard[-1]] == [RESET_LABEL]


def test_menu_shown_with_first_plain_reply_then_with_help():
    app = build_app("123:ABC", Flow(), {1})
    text = _handler(app, "MessageHandler")
    upd, sent = _update(1, "что-то")
    asyncio.run(text.callback(upd, None))
    # У первого сообщения — свои кнопки; меню встаёт под первым без кнопок.
    assert isinstance(sent[0][1], InlineKeyboardMarkup)
    assert isinstance(sent[1][1], ReplyKeyboardMarkup)
    # Если все ответы с кнопками — меню отдельной строкой (один раз).
    app2 = build_app("123:ABC", SimpleNamespace(
        on_text=lambda c, t: [Reply("Вопрос", [[("A", "x:1")]])]), {1})
    upd2, sent2 = _update(1, "что-то")
    asyncio.run(_handler(app2, "MessageHandler").callback(upd2, None))
    assert isinstance(sent2[0][1], InlineKeyboardMarkup)
    assert isinstance(sent2[1][1], ReplyKeyboardMarkup) and "меню" in sent2[1][0]
    upd2, sent2 = _update(1, "ещё")
    asyncio.run(_handler(app2, "MessageHandler").callback(upd2, None))
    assert len(sent2) == 1
    upd, sent = _update(1, "ещё")
    asyncio.run(text.callback(upd, None))
    assert sent[1][1] is None                                 # второй раз не дублируем
    upd, sent = _update(1, "/help")
    asyncio.run(_handler(app, "CommandHandler").callback(upd, SimpleNamespace(args=[])))
    assert isinstance(sent[0][1], ReplyKeyboardMarkup)       # /help — всегда с меню


def test_reset_button_only_in_test_bot():
    wiped = []
    app = build_app("123:ABC", Flow(), {1}, label="🧪 ТЕСТ", reset=wiped.append)
    upd, sent = _update(1, RESET_LABEL)
    asyncio.run(_handler(app, "MessageHandler").callback(upd, None))
    assert "Стереть ВСЕ тестовые данные" in sent[0][0] and wiped == []
    prod = build_app("123:ABC", Flow(), {1})
    upd, sent = _update(1, RESET_LABEL)
    asyncio.run(_handler(prod, "MessageHandler").callback(upd, None))
    assert "Стереть" not in sent[0][0]                       # боевой — обычный текст
