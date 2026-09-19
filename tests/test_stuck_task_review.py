"""Ручной разбор зависших заданий 1С на «Диагностике».

Автоповтор ограничен пятью попытками; дальше решение принимает человек,
посмотревший в 1С. Два исхода различаются не формулировкой, а последствием для
остатка, и в этом вся суть:

  * «1С документ создала» — задание закрывается как проведённое. 1С эту единицу
    у себя уже списала, «в пути» снимается, остаток сходится сам.
  * «документа нет» — 1С единицу НЕ списала, поэтому после сверки остаток
    вырастет на количество задания. Решение опасное: если товар на самом деле
    отгружен, площадки начнут продавать проданное. Потому и отдано человеку.
"""
from datetime import timedelta

import pytest

from app.models import (Barcode, FtpTask, FtpTaskStatus, Platform, PlatformAccount,
                        Product, User)
from app.timeutils import now_utc
from app.workers import ftp_channel as ch
from app.workers.reconciliation import _in_flight_adjustment


def _account(web_db) -> PlatformAccount:
    a = PlatformAccount(platform=Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh")
    web_db.add(a)
    web_db.commit()
    web_db.refresh(a)
    return a


def _product(web_db, uid="u1", barcode="2000932307169") -> Product:
    p = Product(uid_1c=uid, article="2617 C24-2317CQ", size="46", color="NAVY",
                name="Куртка", stock_on_hand=5, reserve=0)
    web_db.add(p)
    web_db.add(Barcode(barcode=barcode, uid_1c=uid))
    web_db.commit()
    return p


def _stuck(web_db, account, *, status=FtpTaskStatus.timeout, age_minutes=120,
           reposts=0, quantity=1, barcode="2000932307169") -> FtpTask:
    t = FtpTask(command="CREATE_MOVEMENT", barcode=barcode, warehouse_from="ЦС Склад",
                warehouse_to="Wildberries_Склад_FBO", quantity=quantity,
                order_id="5638678769", account_id=account.id, status=status,
                repost_count=reposts, is_test=False,
                sent_at=now_utc() - timedelta(minutes=age_minutes))
    web_db.add(t)
    web_db.commit()
    web_db.refresh(t)
    return t


# ------------------------------------------------ кого зовём на разбор

def test_a_stuck_task_is_offered_for_review_while_repost_is_off(web_db):
    """Сегодняшнее состояние боя: идемпотентности в 1С нет, автоповтор выключен.
    Без этой ветки зависшие задания не попадали бы в разбор вовсе."""
    acc = _account(web_db)
    t = _stuck(web_db, acc)

    assert [x.id for x in ch.tasks_needing_review(web_db)] == [t.id]


def test_a_fresh_timeout_is_not_offered(web_db):
    """Опоздавший ответ 1С закрывает такое задание сам — звать человека рано."""
    acc = _account(web_db)
    _stuck(web_db, acc, age_minutes=5)

    assert ch.tasks_needing_review(web_db) == []


def test_with_repost_on_only_an_exhausted_task_is_offered(web_db, monkeypatch):
    monkeypatch.setenv(ch.MOVEMENT_REPOST_ENV, "1")
    acc = _account(web_db)
    _stuck(web_db, acc, reposts=1)
    exhausted = _stuck(web_db, acc, reposts=ch.MAX_REPOSTS)

    assert [x.id for x in ch.tasks_needing_review(web_db)] == [exhausted.id]


def test_a_refusal_is_offered_at_once(web_db):
    """`failed` — 1С сказала ERROR: документа нет, ждать нечего."""
    acc = _account(web_db)
    t = _stuck(web_db, acc, status=FtpTaskStatus.failed, age_minutes=1)

    assert [x.id for x in ch.tasks_needing_review(web_db)] == [t.id]


def test_a_test_task_is_never_offered(web_db):
    acc = _account(web_db)
    t = _stuck(web_db, acc)
    t.is_test = True
    web_db.commit()

    assert ch.tasks_needing_review(web_db) == []


# ------------------------------------------------ последствия решения

def test_document_found_closes_the_task_and_clears_the_in_flight(web_db):
    acc = _account(web_db)
    _product(web_db)
    t = _stuck(web_db, acc)
    assert _in_flight_adjustment(web_db, "u1") == 1

    ch.resolve_stuck_task(web_db, t, document_exists=True, actor="admin")

    web_db.refresh(t)
    assert t.status is FtpTaskStatus.done
    assert _in_flight_adjustment(web_db, "u1") == 0


def test_no_document_also_clears_the_in_flight_so_the_unit_returns(web_db):
    """Ради этого кнопка и нужна: пока задание «в пути», остаток занижен вечно."""
    acc = _account(web_db)
    _product(web_db)
    t = _stuck(web_db, acc, quantity=2)
    assert _in_flight_adjustment(web_db, "u1") == 2

    ch.resolve_stuck_task(web_db, t, document_exists=False, actor="admin")

    web_db.refresh(t)
    assert t.status is FtpTaskStatus.no_document
    assert _in_flight_adjustment(web_db, "u1") == 0


def test_the_decision_and_its_author_are_recorded(web_db):
    acc = _account(web_db)
    t = _stuck(web_db, acc)

    ch.resolve_stuck_task(web_db, t, document_exists=False, actor="petrov")

    web_db.refresh(t)
    assert t.result_status == "NO_DOCUMENT"
    assert "petrov" in t.result_detail and "не найден" in t.result_detail
    assert t.completed_at is not None


def test_a_resolved_task_is_never_reposted(web_db, monkeypatch):
    monkeypatch.setenv(ch.MOVEMENT_REPOST_ENV, "1")
    acc = _account(web_db)
    t = _stuck(web_db, acc)
    ch.resolve_stuck_task(web_db, t, document_exists=False, actor="admin")

    ch.repost_stuck_movements(web_db)

    web_db.refresh(t)
    assert t.status is FtpTaskStatus.no_document


# ------------------------------------------------ страница и кнопки

def test_the_page_lists_the_stuck_task_with_what_is_needed_to_decide(logged_in_client, web_db):
    acc = _account(web_db)
    _product(web_db)
    _stuck(web_db, acc)

    page = logged_in_client.get("/diagnostics")

    assert "Зависшие задания 1С" in page.text
    assert "2617 C24-2317CQ" in page.text        # что за товар
    assert "2000932307169" in page.text          # баркод
    assert "1С документ создала" in page.text and "документа нет" in page.text


def test_the_page_says_when_there_is_nothing_to_review(logged_in_client, web_db):
    page = logged_in_client.get("/diagnostics")

    assert "Зависших заданий нет." in page.text


def test_the_button_closes_the_task(logged_in_client, web_db):
    acc = _account(web_db)
    _product(web_db)
    t = _stuck(web_db, acc)

    logged_in_client.post(f"/diagnostics/stuck-tasks/{t.id}/resolve",
                          data={"document_exists": "yes"}, follow_redirects=True)

    web_db.expire_all()
    assert web_db.query(FtpTask).filter(FtpTask.id == t.id).first().status is FtpTaskStatus.done


def test_an_unstated_decision_is_refused_not_guessed(logged_in_client, web_db):
    """Исходы различаются последствием для остатка — угадывать тут нельзя."""
    acc = _account(web_db)
    t = _stuck(web_db, acc)

    r = logged_in_client.post(f"/diagnostics/stuck-tasks/{t.id}/resolve",
                              data={"document_exists": ""}, follow_redirects=True)

    web_db.expire_all()
    assert web_db.query(FtpTask).filter(FtpTask.id == t.id).first().status is FtpTaskStatus.timeout
    assert "Не указано" in r.text


def test_an_already_closed_task_is_not_reopened(logged_in_client, web_db):
    acc = _account(web_db)
    t = _stuck(web_db, acc, status=FtpTaskStatus.done)

    logged_in_client.post(f"/diagnostics/stuck-tasks/{t.id}/resolve",
                          data={"document_exists": "no"}, follow_redirects=True)

    web_db.expire_all()
    assert web_db.query(FtpTask).filter(FtpTask.id == t.id).first().status is FtpTaskStatus.done


def test_resolving_is_written_to_the_audit_log(logged_in_client, web_db):
    from app.models import AuditLog
    acc = _account(web_db)
    t = _stuck(web_db, acc)

    logged_in_client.post(f"/diagnostics/stuck-tasks/{t.id}/resolve",
                          data={"document_exists": "no"}, follow_redirects=True)

    entries = web_db.query(AuditLog).filter(AuditLog.action == "stuck_task_resolved").all()
    assert len(entries) == 1 and "НЕ найден" in entries[0].details
