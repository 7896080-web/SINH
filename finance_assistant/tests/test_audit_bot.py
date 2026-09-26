"""Регрессии по аудиту Telegram-обвязки (без сети)."""
import asyncio

from telegram import Update

from finance.bot import MAX_TEXT, _split, _units, build_app


def _msg(chat_type="private", text="/list", user=1, edited=False, bot=None):
    body = {"message_id": 1, "date": 0, "text": text,
            "chat": {"id": 5, "type": chat_type},
            "from": {"id": user, "is_bot": False, "first_name": "x"}}
    if text.startswith("/"):
        body["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    key = "edited_message" if edited else "message"
    return Update.de_json({"update_id": 1, key: body}, bot)


class RecordingFlow:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, a)) or []


def test_edited_messages_not_handled():
    app = build_app("123:ABC", RecordingFlow(), {1})
    text = dict(text="3500 такси", bot=app.bot)
    assert not any(h.check_update(_msg(edited=True, **text)) for h in app.handlers[0])
    assert any(h.check_update(_msg(**text)) for h in app.handlers[0])


def test_group_chat_ignored_even_for_allowed_user():
    flow = RecordingFlow()
    app = build_app("123:ABC", flow, {1})
    text = next(h for h in app.handlers[0] if h.check_update(_msg(text="такси", bot=app.bot)))
    asyncio.run(text.callback(_msg(chat_type="group", text="такси", bot=app.bot), None))
    assert flow.calls == []


def test_split_long_line_and_emoji():
    parts = _split("😀" * 3000 + "\n" + "x" * 9000)
    assert all(_units(p) <= MAX_TEXT for p in parts)
    assert "".join(parts).replace("\n", "") == "😀" * 3000 + "x" * 9000
