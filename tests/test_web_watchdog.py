"""Второй сторож — про ВЕБ-СЛУЖБУ, и он закрывает единственную оставшуюся дыру.

`ping_alive` покрывает смерть воркера: он молчит — внешний сервис будит. А вот
обратный случай, «умер веб, воркер жив», изнутри не видно НИЧЕМ: воркер
пингует сторожа, `job_alerts` спрашивает состояние функцией `snapshot(db)` (то
есть читает базу, а не ходит по HTTP) и видит все задания свежими. Остатки
синхронизируются дальше, денег никто не теряет — слепнет человек. На боевом
сервере порт наружу не открыт, внешним монитором веб не достать, поэтому за
него говорит воркер: он жив, и подтверждать может.

Главное свойство, которое здесь и проверяется: **не ответил веб — НЕ ПИНГУЕМ**.
Пинг означает «подтверждаю, что беды нет», и подтверждать становится нечего.
И второе: у каждого сигнала один смысл — два URL, два вопроса; подмешай мы
«и веб отвечает» в первый пинг, его молчание стало бы неотличимо от падения
воркера.
"""

import pytest

from app import alerts
from app.settings_store import set_value
from app.workers import scheduler
from app.models import WorkerHeartbeat

WATCH = "https://hc-ping.test/web"


WORKER_WATCH = "https://hc-ping.test/worker"


class _Calls(list):
    """Куда ходили — по порядку. Предмет проверки именно в этом.

    `down` — адреса, которые сегодня не отвечают. Набором, а не флагами: иначе
    тест «упали обе» молча проверял бы одно падение, что уже и вышло.
    """

    def __init__(self):
        super().__init__()
        self.down = set()

    def get(self, url, timeout=None):
        self.append(url)
        if url in self.down:
            raise ConnectionError("connection refused")

        class _Ok:
            @staticmethod
            def raise_for_status():
                return None
        return _Ok()


@pytest.fixture()
def calls(db, monkeypatch):
    out = _Calls()
    monkeypatch.setattr(alerts.requests, "get", out.get)
    # Задание открывает СВОЮ сессию: без подмены оно ушло бы в другую базу, и
    # проверка отметки стерегла бы пустоту.
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    set_value(db, "WEB_HEARTBEAT_URL", WATCH)
    db.commit()
    return out


def test_a_live_web_is_confirmed_to_the_watchdog(db, calls):
    """Веб ответил — подтверждаем. Порядок важен: сначала спрашиваем, потом
    подтверждаем, иначе подтверждение ничего не значит."""
    assert alerts.ping_web_alive(db) == ""

    assert calls == [alerts.WEB_PROBE_URL, WATCH]


def test_a_dead_web_is_NOT_confirmed(db, calls):
    """Сердце всей затеи. Веб не ответил — сторожа НЕ дёргаем: пусть замолчит и
    разбудит человека сам. Пингуй мы всё равно, второй чек был бы украшением."""
    calls.down.add(alerts.WEB_PROBE_URL)

    problem = alerts.ping_web_alive(db)

    assert calls == [alerts.WEB_PROBE_URL], "пинг ушёл при мёртвом вебе"
    assert "веб-служба не отвечает" in problem


def test_an_unset_url_pings_nothing_and_is_not_an_error(db, monkeypatch):
    """Свежая установка: второго чека ещё нет. Это штатно, а не поломка."""
    out = _Calls()
    monkeypatch.setattr(alerts.requests, "get", out.get)

    assert alerts.ping_web_alive(db) == ""
    assert out == []


def test_a_live_web_that_could_not_be_confirmed_says_so(db, calls):
    """Веб жив, а сказать об этом не вышло: молчание сторожа прочтётся как
    «веб умер», то есть тревога будет ЛОЖНОЙ. Это обязано быть видно."""
    calls.down.add(WATCH)

    problem = alerts.ping_web_alive(db)

    assert "сторож веб-службы не ответил" in problem
    assert "веб-служба не отвечает" not in problem     # веб-то как раз ответил


# ------------------------------------------------- связь с заданием

def test_the_watchdog_job_pings_both(db, calls):
    """Оба сторожа дёргает одно задание — полярность у них одна. Смыслы при
    этом раздельные: два URL, два вопроса."""
    set_value(db, "ALERT_HEARTBEAT_URL", WORKER_WATCH)
    db.commit()

    scheduler.job_watchdog()

    assert WORKER_WATCH in calls
    assert alerts.WEB_PROBE_URL in calls
    assert WATCH in calls


def test_a_dead_web_reaches_the_diagnostics_note(db, calls):
    """У текста обязан быть читатель: оговорка успешной отметки, её показывает
    «Диагностика». Без неё единственным следом осталась бы тишина сторожа,
    которую не с чем сопоставить."""
    calls.down.add(alerts.WEB_PROBE_URL)

    scheduler.job_watchdog()

    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "watchdog").first()
    assert row.last_success is True, "не доехавший пинг — не поломка задания"
    assert "веб-служба не отвечает" in (row.last_error or "")


def test_both_notes_fit_in_one_mark(db, calls):
    """Две беды сразу не должны вытеснять друг друга."""
    set_value(db, "ALERT_HEARTBEAT_URL", WORKER_WATCH)
    db.commit()
    calls.down |= {alerts.WEB_PROBE_URL, WORKER_WATCH}

    scheduler.job_watchdog()

    text = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "watchdog").first().last_error
    assert "внешний сторож не ответил" in text
    assert "веб-служба не отвечает" in text


def test_the_page_offers_the_second_watchdog(logged_in_client):
    """Настройка заводится СТРАНИЦЕЙ, а не файлом: правка `.env` без
    перезапуска не действует, а про перезапуск забывают."""
    page = logged_in_client.get("/notifications").text

    assert "WEB_HEARTBEAT_URL" in page
    assert "Пинг сторожа веб-службы" in page
