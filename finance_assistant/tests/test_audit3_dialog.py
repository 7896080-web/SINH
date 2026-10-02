"""Регрессии третьего аудита: месяц глазами владельца (альбомы, очередь, меню)."""
import io
from datetime import date

import openpyxl

from conftest import CHAT, answer, payment, png
from finance.flow import MENU_RECORDS, Flow
from finance.reconcile import summarize
from finance.report import month_text, month_xlsx
from finance.storage import BUSINESS_ACCOUNT, PERSONAL


def buttons(reply):
    return [data for row in reply.buttons for _, data in row]


def items(n, **over):
    return [dict(files=[png()], caption="", filename="", file_ids=[]) for _ in range(n)]


def test_album_not_stuck_behind_old_question(env):
    """Висит вопрос по одной операции — новый альбом всё равно записывается сразу."""
    db, rec, flow = env
    rec.payments.append(payment(card_last4="", bank="", merchant="Старый"))
    [q] = flow.on_files(CHAT, [png()], "")
    assert "С какой карты" in q.text
    rec.payments += [payment(amount=str(100 + i), merchant=f"M{i}") for i in range(5)]
    replies = flow.on_batch(CHAT, items(5))
    assert len(db.expenses("2026-09")) == 5                       # альбом записан
    assert "С какой карты" in replies[-1].text and "Старый" in replies[-1].text


def test_menu_cancel_drops_only_current_question(env):
    db, rec, flow = env
    rec.payments += [payment(card_last4="", bank="", merchant=f"Q{i}") for i in range(3)]
    flow.on_batch(CHAT, items(3))
    assert len(db.get_state(CHAT)["drafts"]) == 3
    replies = flow.on_button(CHAT, "m:cancel")
    assert "не записываю" in replies[0].text and len(db.get_state(CHAT)["drafts"]) == 2
    [r] = flow.on_command(CHAT, "cancel")                          # /cancel — всё, но честно
    assert "Не записано операций: 2" in r.text


def test_text_while_waiting_for_button_repeats_question(env):
    db, rec, flow = env
    rec.payments.append(payment(card_last4="", bank=""))
    flow.on_files(CHAT, [png()], "")
    r = flow.on_text(CHAT, "это реклама")
    assert "Ответьте" in r[0].text and "С какой карты" in r[-1].text
    assert len(rec.calls) == 1                                     # Claude не дёргали


def test_answers_during_statement_go_to_question(env):
    db, rec, flow = env
    rec.payments.append(payment(date=""))
    flow.on_files(CHAT, [png()], "")
    flow.on_button(CHAT, "s:m:2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    calls = len(rec.calls)
    flow.on_text(CHAT, "вчера")                                    # ответ на вопрос о дате
    assert len(rec.calls) == calls and db.expenses("2026-09")[0].op_date == "2026-09-25"


def test_gotovo_text_finishes_statement(env):
    db, rec, flow = env
    flow.on_button(CHAT, "s:m:2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    flow.on_text(CHAT, "Готово")
    assert "statement" not in db.get_state(CHAT) and rec.calls == []


def test_purpose_same_and_business_without_category(env):
    db, rec, flow = env
    rec.payments.append(payment(looks_personal=True))
    flow.on_files(CHAT, [png()], "")
    answer(flow, "d:purpose:personal")
    e = db.expenses("2026-09")[0]
    assert flow.on_button(CHAT, f"e:purpose:{e.id}:personal")[0].text.startswith("Без изменений")
    db.update_expense(e.id, category_id=None)
    [pick] = flow.on_button(CHAT, f"e:purpose:{e.id}:business")
    assert f"e:keep:{e.id}" in buttons(pick)
    assert db.expense(e.id).purpose == PERSONAL                    # пока статья не выбрана


def test_records_early_in_month_show_previous(tmp_path):
    from conftest import FakeRecognizer
    from finance.storage import Storage
    db = Storage(str(tmp_path / "f.db"))
    rec = FakeRecognizer()
    flow = Flow(db, rec, str(tmp_path / "r"), today=lambda: date(2026, 10, 2))
    db.add_card("Сбер", "1111", "Сбер")
    rec.payments.append(payment(date="2026-09-28"))
    flow.on_files(CHAT, [png()], "")
    [r] = flow.on_text(CHAT, MENU_RECORDS)
    assert "сентябрь" in r.text
    [v] = flow.on_button(CHAT, "m:vypiska")
    assert "сентябрь" in v.text.lower() and "m:vyp:2026-08" in buttons(v)


def test_month_excel_sheet_adds_up_with_business_account(env):
    db, _, _ = env
    psb = db.add_card("ПСБ", "4987", "ПСБ", kind=BUSINESS_ACCOUNT).id
    sid = db.statement_id(psb, "2026-09")
    db.add_statement_lines(sid, [dict(op_date="2026-09-05", amount=200_000, direction="out",
                                      description="Без статьи", own_transfer=False)])
    s = summarize(db, "2026-09")
    wb = openpyxl.load_workbook(io.BytesIO(month_xlsx(s)))
    rows = list(wb["Бизнес-расходы"].iter_rows(min_row=2, values_only=True))
    assert sum(r[2] for r in rows) == s.business / 100


def test_personal_marked_shown_without_statement(env):
    db, rec, flow = env
    rec.payments.append(payment(looks_personal=True, amount="1200"))
    flow.on_files(CHAT, [png()], "")
    answer(flow, "d:purpose:personal")
    s = summarize(db, "2026-09")
    assert s.personal == 120_000
    assert "записано вами 1 200,00 ₽" in month_text(s)
