"""Защита от повторов: тот же файл, похожая запись, перевод с той же стороны."""
from conftest import CHAT, png, payment, answer
from test_sverka import PDF, op, statement
from test_transfers import cid, move, own  # noqa: F401 — фикстура own


def test_same_screenshot_not_sent_to_claude_again(env):
    db, rec, flow = env
    shot = png()
    rec.payments.append(payment())
    flow.on_files(CHAT, [shot], "")
    note, saved = flow.on_files(CHAT, [shot], "")
    assert "уже присылали" in note.text and saved.text.startswith("✅ Записано №1")
    assert len(rec.calls) == 1  # второй раз в Claude не отправляли
    assert len(db.expenses("2026-09")) == 1


def test_forwarded_screenshot_recognised_by_telegram_id(env):
    db, rec, flow = env
    rec.payments.append(payment())
    flow.on_files(CHAT, [png()], "", file_ids=["AQADtg"])
    # Telegram пережал картинку — байты другие, но id файла тот же.
    [note, _] = flow.on_files(CHAT, [png()], "", file_ids=["AQADtg"])
    assert "уже присылали" in note.text and len(rec.calls) == 1


def test_same_screenshot_while_first_still_in_queue(env):
    db, rec, flow = env
    shot = png()
    rec.payments.append(payment(card_last4="", bank=""))
    [q] = flow.on_files(CHAT, [shot], "")
    assert "С какой карты" in q.text
    [r] = flow.on_files(CHAT, [shot], "")
    assert "уже в работе" in r.text and len(rec.calls) == 1


def test_deleted_record_can_be_sent_again(env):
    db, rec, flow = env
    shot = png()
    rec.payments += [payment(), payment()]
    flow.on_files(CHAT, [shot], "")
    flow.on_button(CHAT, "e:del:1")
    [r] = flow.on_files(CHAT, [shot], "")
    assert r.text.startswith("✅ Записано") and len(rec.calls) == 2
    assert len(db.expenses("2026-09")) == 1


def test_similar_expense_date_shifted_by_a_day(env):
    db, rec, flow = env
    rec.payments += [payment(date="2026-09-20"), payment(date="2026-09-21"),
                     payment(date="2026-09-23")]
    flow.on_files(CHAT, [png()], "")
    [q] = flow.on_files(CHAT, [png()], "")  # другой скриншот той же оплаты, дата на день позже
    assert "Похожая операция уже записана" in q.text and "№1" in q.text
    answer(flow, "d:skip")
    [r] = flow.on_files(CHAT, [png()], "")  # через 3 дня — это уже другая оплата
    assert r.text.startswith("✅")
    assert len(db.expenses("2026-09")) == 2


def test_similar_expense_same_merchant_other_card(env):
    db, rec, flow = env
    rec.payments += [payment(merchant="ООО «СДЭК-Глобал»"),
                     payment(card_last4="2222", bank="Т-Банк", merchant="сдэк глобал"),
                     payment(card_last4="3333", bank="Альфа", merchant="Озон")]
    flow.on_files(CHAT, [png()], "")
    [q] = flow.on_files(CHAT, [png()], "")
    assert "Похожая операция" in q.text
    answer(flow, "d:dup:ok")  # это правда другая оплата
    [r] = flow.on_files(CHAT, [png()], "")  # другой получатель и карта — не спрашиваем
    assert r.text.startswith("✅")
    assert len(db.expenses("2026-09")) == 3


def test_same_transfer_same_side_asks_instead_of_dropping(own):
    """Два настоящих перевода на одну сумму в один день не должны склеиться молча."""
    db, rec, flow = own
    rec.payments += [move(), move(), move()]
    flow.on_files(CHAT, [png()], "")
    [q] = flow.on_files(CHAT, [png()], "")
    assert "Похожий перевод уже записан: П1" in q.text
    [saved] = answer(flow, "d:dup:ok")
    assert saved.text.startswith("🔁 Перевод между своими счетами П2")
    [q] = flow.on_files(CHAT, [png()], "")
    answer(flow, "d:skip")
    assert len(db.transfers("2026-09")) == 2


def test_other_side_merges_only_once(own):
    db, rec, flow = own
    incoming = dict(direction="in", card_last4="2222", bank="ВТБ",
                    counterparty_last4="1111", counterparty_bank="Россия")
    rec.payments += [move(), move(**incoming), move(**incoming)]
    flow.on_files(CHAT, [png()], "")
    [merged] = flow.on_files(CHAT, [png()], "")
    assert "вторая сторона перевода П1" in merged.text
    # Третий скриншот — снова зачисление: обе стороны уже есть, значит спрашиваем.
    [q] = flow.on_files(CHAT, [png()], "")
    assert "Похожий перевод уже записан" in q.text
    assert len(db.transfers("2026-09")) == 1


def test_screenshot_of_other_side_remembered(own):
    db, rec, flow = own
    rec.payments += [move(), move(direction="in", card_last4="2222", bank="ВТБ",
                                  counterparty_last4="1111", counterparty_bank="Россия")]
    flow.on_files(CHAT, [png()], "")
    second = png()
    flow.on_files(CHAT, [second], "")
    note, saved = flow.on_files(CHAT, [second], "")
    assert "уже присылали" in note.text and "П1" in saved.text


def test_same_statement_file_skipped_without_claude(env):
    db, rec, flow = env
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    rec.statements.append(statement([op("2026-09-02", "100")]))
    flow.on_files(CHAT, [PDF], "")
    [r] = flow.on_files(CHAT, [PDF], "")
    assert "уже загружен по карте Сбер" in r.text
    assert len(rec.calls) == 1
    # «Загрузить заново» сбрасывает и отпечатки — тот же файл можно прислать снова.
    flow.on_command(CHAT, "done")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    flow.on_button(CHAT, f"s:reset:{db.cards()[0].id}:2026-09")
    rec.statements.append(statement([op("2026-09-02", "100")]))
    [r] = flow.on_files(CHAT, [PDF], "")
    assert "Принято операций: 1" in r.text


def test_same_statement_file_for_other_card_warns(env):
    db, rec, flow = env
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    rec.statements += [statement([op("2026-09-02", "100")]), statement([op("2026-09-02", "100")])]
    flow.on_files(CHAT, [PDF], "")
    flow.on_command(CHAT, "done")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[1].id}")
    [r] = flow.on_files(CHAT, [PDF], "")
    assert "Этот же файл раньше загружали по карте Сбер" in r.text
