"""Уведомления наружу — единственный путь, которым система зовёт человека.

Проверяется не «отправилось ли», а то, ради чего механизм вообще имеет смысл:
что он позовёт на настоящей поломке, НЕ позовёт на ерунде, не превратится в
поток одинаковых сообщений и скажет «отбой», когда всё прошло. Канал, которому
перестали верить, не работает вообще — не наполовину.
"""
from datetime import timedelta

import pytest

from app import alerts
from app.models import (AlertState, DispatchQueueItem, DispatchStatus, Platform,
                        PlatformAccount, Product, SyncSetting, WorkerHeartbeat)
from app.routers.health import SCHEDULER_START_MARKER
from app.timeutils import now_utc
from tests.factories import make_account


@pytest.fixture()
def channel(monkeypatch):
    """Подставной канал: запоминает отправленное вместо похода в сеть."""
    outbox = []

    def fake_deliver(db, subject, body):
        outbox.append((subject, body))
        return ["telegram"], []

    monkeypatch.setattr(alerts, "configured_channels", lambda db: ["telegram"])
    monkeypatch.setattr(alerts, "deliver", fake_deliver)
    return outbox


def _healthy(db):
    """Система, у которой всё в порядке: планировщик стартовал минуту назад."""
    db.add(WorkerHeartbeat(worker_name=SCHEDULER_START_MARKER,
                           last_run_at=now_utc() - timedelta(minutes=1),
                           last_success=True))
    db.add(WorkerHeartbeat(worker_name="dispatch", last_run_at=now_utc(),
                           last_success=True))
    db.commit()


def _critical_finding(db):
    """Настоящая критичная находка: заказы не проводятся."""
    db.add(WorkerHeartbeat(worker_name="poll_orders_account_1",
                           last_run_at=now_utc(), last_success=True,
                           last_error="заказов не проведено: 3 — IntegrityError"))
    db.commit()


# --------------------------------------------------------------------------
# Зовёт, когда надо
# --------------------------------------------------------------------------

def test_a_critical_finding_wakes_a_human(db, channel):
    """Критичная находка — это деньги, идущие не туда прямо сейчас."""
    _healthy(db)
    _critical_finding(db)

    stats = alerts.run_alert_cycle(db)

    assert stats["action"] == "alarm"
    assert len(channel) == 1
    subject, body = channel[0]
    assert "Требует внимания" in subject
    # Следствие, а не факт — то же правило, что у отчёта: «заказов не
    # проводится 3» само по себе человеку ничего не говорит.
    assert "оверселл" in body


def test_a_dead_worker_wakes_a_human(db, channel):
    """Задание встало — остатки перестают синхронизироваться молча."""
    db.add(WorkerHeartbeat(worker_name=SCHEDULER_START_MARKER,
                           last_run_at=now_utc() - timedelta(hours=2),
                           last_success=True))
    db.add(WorkerHeartbeat(worker_name="dispatch",
                           last_run_at=now_utc() - timedelta(hours=1),
                           last_success=True))
    db.commit()

    stats = alerts.run_alert_cycle(db)

    assert stats["action"] == "alarm"
    assert "dispatch" in channel[0][1]
    assert "не синхронизируются" in channel[0][1]


# --------------------------------------------------------------------------
# Не зовёт, когда не надо
# --------------------------------------------------------------------------

def test_a_healthy_system_stays_silent(db, channel):
    """Молчание на исправной системе — обязательное свойство.

    Сообщение «всё хорошо» на системе, которая и так была в порядке, — спам,
    обесценивающий настоящее.
    """
    _healthy(db)

    assert alerts.run_alert_cycle(db)["action"] == "none"
    assert channel == []


def test_a_warning_never_wakes_anyone(db, channel):
    """Жёлтых находок в исправной системе бывает несколько штук постоянно.

    Разбудив человека на жёлтом один раз, мы научим его не читать и красное —
    и тогда канал перестанет работать весь, а не наполовину.
    """
    _healthy(db)
    db.add(Product(uid_1c="u1", article="A-1", stock_on_hand=-5))
    db.commit()

    from app import report
    levels = {f.level for f in report.collect_findings(db)}
    assert report.WARNING in levels and report.CRITICAL not in levels

    assert alerts.run_alert_cycle(db)["action"] == "none"
    assert channel == []


def test_the_same_trouble_is_not_repeated_every_cycle(db, channel):
    """Задание смотрит каждые пять минут, а поломка держится часами.

    Пять одинаковых сообщений в час — вернейший способ добиться, чтобы
    уведомления отключили, и тогда молчать будет уже всё.
    """
    _healthy(db)
    _critical_finding(db)

    alerts.run_alert_cycle(db)
    db.commit()
    second = alerts.run_alert_cycle(db)

    assert second["action"] == "quiet"
    assert len(channel) == 1


def test_a_new_trouble_is_reported_even_while_the_old_one_stands(db, channel):
    """Картина изменилась — значит случилось что-то ещё, и молчать нельзя."""
    _healthy(db)
    _critical_finding(db)
    alerts.run_alert_cycle(db)
    db.commit()

    # Добавилась вторая критичная находка.
    account = make_account(db)
    db.add(Product(uid_1c="u9", article="A-9", stock_on_hand=5,
                   broadcast_enabled=True))
    db.commit()

    stats = alerts.run_alert_cycle(db)

    assert stats["action"] == "alarm"
    assert len(channel) == 2


def test_a_standing_trouble_is_repeated_after_the_timer(db, channel):
    """Висящая поломка не должна забыться: напоминаем по долгому таймеру."""
    _healthy(db)
    _critical_finding(db)
    alerts.run_alert_cycle(db)
    db.commit()

    state = db.query(AlertState).first()
    state.last_sent_at = now_utc() - alerts.repeat_after(db) - timedelta(minutes=1)
    db.commit()

    stats = alerts.run_alert_cycle(db)

    assert stats["action"] == "alarm"
    assert "Всё ещё" in channel[1][0]


# --------------------------------------------------------------------------
# Отбой
# --------------------------------------------------------------------------

def test_recovery_is_announced_once(db, channel):
    """Человек, получивший тревогу, обязан узнать, что она кончилась.

    Иначе он либо идёт проверять руками каждый раз, либо перестаёт верить и
    первому сообщению.
    """
    _healthy(db)
    _critical_finding(db)
    alerts.run_alert_cycle(db)
    db.commit()

    hb = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "poll_orders_account_1").first()
    hb.last_error = None
    db.commit()

    first = alerts.run_alert_cycle(db)
    db.commit()
    second = alerts.run_alert_cycle(db)

    assert first["action"] == "clear"
    assert "Отбой" in channel[1][0]
    assert second["action"] == "none", "отбой шлётся один раз, а не каждый цикл"


def test_there_is_no_all_clear_without_an_alarm(db, channel):
    """Отбой без тревоги — это сообщение ни о чём."""
    _healthy(db)
    db.add(AlertState(key=alerts.STATE_KEY, level="clear"))
    db.commit()

    assert alerts.run_alert_cycle(db)["action"] == "none"
    assert channel == []


# --------------------------------------------------------------------------
# Сбой канала
# --------------------------------------------------------------------------

def test_an_undelivered_alarm_is_retried_next_cycle(db, monkeypatch):
    """Не доехало — не запоминаем.

    Иначе тревога, потерянная из-за недоступного Telegram, была бы потеряна
    НАВСЕГДА: следующий цикл счёл бы её уже сообщённой.
    """
    _healthy(db)
    _critical_finding(db)
    attempts = []

    monkeypatch.setattr(alerts, "configured_channels", lambda db: ["telegram"])
    monkeypatch.setattr(alerts, "deliver",
                        lambda db, s, b: (attempts.append(s), ([], ["telegram: timeout"]))[1])

    first = alerts.run_alert_cycle(db)
    db.commit()
    second = alerts.run_alert_cycle(db)

    assert first["failed"] == 1
    assert second["action"] == "alarm", "повторить обязаны — сообщение не дошло"
    assert len(attempts) == 2
    assert (db.query(AlertState).first().signature or "") == ""


def test_one_broken_channel_does_not_stop_the_other(db, monkeypatch):
    """Тревога, потерянная из-за одного упавшего канала, — тревога, которой не было."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ALERT_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("ALERT_EMAIL_TO", "a@example")

    def boom(cfg, subject, body):
        raise RuntimeError("сеть недоступна")

    calls = []
    monkeypatch.setattr(alerts, "_send_telegram", boom)
    monkeypatch.setattr(alerts, "_send_email", lambda cfg, s, b: calls.append(s))

    sent, failed = alerts.deliver(db, "тема", "текст")

    assert sent == ["email"]
    assert len(failed) == 1 and "telegram" in failed[0]
    assert calls == ["тема"]


def test_telegram_ok_false_is_not_a_delivery(db, monkeypatch):
    """200 с `ok: false` — обычный для Telegram способ отказать.

    Считать это доставкой значит молча потерять тревогу.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": False, "description": "chat not found"}

    monkeypatch.setattr(alerts.requests, "post", lambda *a, **k: Resp())

    sent, failed = alerts.deliver(db, "тема", "текст")

    assert sent == []
    assert "chat not found" in failed[0]


# --------------------------------------------------------------------------
# Настройка
# --------------------------------------------------------------------------

def test_nothing_configured_means_nothing_is_sent_and_it_is_said_out_loud(db, monkeypatch):
    """Свежая установка никого не зовёт — и об этом обязано быть сказано.

    Канал, про который никто не знает, что он выключен, хуже отсутствующего.
    """
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "ALERT_SMTP_HOST", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)
    _healthy(db)
    _critical_finding(db)

    assert alerts.run_alert_cycle(db)["action"] == "not_configured"


def test_the_page_says_when_no_one_will_be_called(logged_in_client, web_db, monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "ALERT_SMTP_HOST", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)

    page = logged_in_client.get("/diagnostics").text

    assert "Сейчас система никого не позовёт" in page


def test_the_test_button_refuses_when_nothing_is_configured(logged_in_client, monkeypatch):
    """Отказ с причиной, а не зелёное «отправлено» в никуда."""
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "ALERT_SMTP_HOST", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)

    page = logged_in_client.post("/diagnostics/alerts/test",
                                 follow_redirects=True).text

    assert "не настроены" in page


def test_the_test_button_does_not_eat_the_next_real_alarm(db, channel, monkeypatch):
    """Проверка связи — не тревога, и запоминать её как сообщённую картину нельзя.

    Иначе настоящая тревога следом сочлась бы повтором и не ушла.
    """
    _healthy(db)
    _critical_finding(db)

    alerts.deliver(db, "[Sync Admin] Проверка связи", "пробное")
    assert db.query(AlertState).count() == 0

    assert alerts.run_alert_cycle(db)["action"] == "alarm"


# --------------------------------------------------------------------------
# Внешний сторож: единственное, что переживает смерть воркера
# --------------------------------------------------------------------------

def test_the_watchdog_is_pinged_on_every_run(db, monkeypatch):
    """Задание `alerts` живёт ВНУТРИ воркера.

    Умер воркер — умерли и уведомления, и главный сценарий («ночью обе службы
    легли») остался бы непокрытым. Изнутри это не решается в принципе: процесс,
    которого нет, не может сообщить, что его нет. Поэтому обратная полярность —
    регулярно говорим «жив», а молчание разбирает внешний сервис.
    """
    called = []

    class Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setenv("ALERT_HEARTBEAT_URL", "https://hc.example/ping/abc")
    monkeypatch.setattr(alerts.requests, "get",
                        lambda url, **kw: (called.append(url), Resp())[1])

    assert alerts.ping_alive(db) == ""
    assert called == ["https://hc.example/ping/abc"]


def test_no_watchdog_configured_is_not_an_error(db, monkeypatch):
    """Сторож необязателен: без него работают обычные уведомления."""
    monkeypatch.delenv("ALERT_HEARTBEAT_URL", raising=False)
    assert alerts.ping_alive(db) == ""


def test_a_silent_watchdog_is_reported(db, monkeypatch):
    """Сторож, о котором мы думаем, что он сторожит, хуже отсутствующего."""
    monkeypatch.setenv("ALERT_HEARTBEAT_URL", "https://hc.example/ping/abc")

    def boom(url, **kw):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(alerts.requests, "get", boom)

    problem = alerts.ping_alive(db)

    assert "сеть недоступна" in problem


def test_the_watchdog_is_pinged_even_when_the_system_is_broken(db, monkeypatch):
    """Сторож отвечает на ОДИН вопрос — «жив ли воркер».

    Пропусти он пинг из-за находок внутри — человек получил бы от сторожа
    сигнал, неотличимый от упавшей службы, а о настоящей беде ему и так скажет
    обычное уведомление.
    """
    from app.workers import scheduler

    _healthy(db)
    _critical_finding(db)
    pings = []

    monkeypatch.setattr(alerts, "ping_alive", lambda db: (pings.append(1), "")[1])
    monkeypatch.setattr(alerts, "configured_channels", lambda db: ["telegram"])
    monkeypatch.setattr(alerts, "deliver", lambda db, s, b: (["telegram"], []))
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    # Сторож — ОТДЕЛЬНОЕ задание, а не строчка внутри `job_alerts`: пока он жил
    # там, он наследовал двенадцатиминутную задержку первого прогона, заведённую
    # под каналы. Само свойство от переноса не изменилось, и проверяем мы
    # по-прежнему его: пинг уходит при любом состоянии системы — здесь она
    # заведомо сломана (критичная находка на месте).
    scheduler.job_watchdog()

    assert pings == [1]
