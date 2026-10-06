"""Площадка замолчала — кабинет остаётся включённым. Он сам не включится.

06.10 в 02:29:04 предохранитель погасил ТРИ кабинета WB одной секундой: WB
перестал отвечать на `/api/v3/orders/new` (`Read timed out (read timeout=30)`),
опрос упал пять циклов подряд — и защита от сломанных ключей выключила три
кабинета с исправными ключами. Озон и Кит в это же время работали штатно, то
есть сеть с сервера была жива.

Цена несимметрична, и в этом вся суть правила. Недоступность площадки проходит
САМА: через минуту WB ответил, и дальше всё работало бы без человека. А
выключенный кабинет сам не включается никогда: его per-account задания сняты,
заказы не опрашиваются, остатки не рассылаются — и `/health` при этом ЗЕЛЁНЫЙ,
потому что выключенные кабинеты он пропускает намеренно. Единственным сигналом
остаётся часовой отчёт. Ночью это значит часы простоя: заказы с площадки
приходят, остаток у нас не списывается, наружу уходит завышенное число.

Поэтому предохранитель теперь спрашивает ровно один вопрос: переживёт ли этот
отказ повтор. Переживёт (молчание, 429, 5xx) — пишем причину, но не гасим.
Не переживёт (401, 403, 404 — ключ отозван, прав нет, ручки нет) — гасим, ради
этого он и заведён.
"""
import pytest
import requests

from app.models import Platform, PlatformAccount, WorkerHeartbeat
from app.workers import scheduler
from app.workers.circuit_breaker import (FAILURE_THRESHOLD, record_failure,
                                         survives_a_retry)
from tests.factories import make_account

TIMEOUT = requests.exceptions.ReadTimeout(
    "HTTPSConnectionPool(host='marketplace-api.wildberries.ru', port=443): "
    "Read timed out. (read timeout=30)")


def _http(code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = code
    return requests.HTTPError(response=response)


@pytest.mark.parametrize("name,error", [
    ("молчание площадки", TIMEOUT),
    ("обрыв соединения", requests.exceptions.ConnectionError("reset")),
    ("лимит запросов", _http(429)),
    ("площадке плохо", _http(500)),
    ("площадка на обслуживании", _http(503)),
])
def test_an_outage_survives_a_retry(name, error):
    """Всё это пройдёт само — значит гасить кабинет нельзя."""
    assert survives_a_retry(error) is True, name


@pytest.mark.parametrize("name,error", [
    ("ключ отозван", _http(401)),
    ("прав нет", _http(403)),
    ("ручки нет", _http(404)),
])
def test_a_broken_key_does_not_survive_a_retry(name, error):
    """Ради этого предохранитель и заведён: долбить такое бессмысленно."""
    assert survives_a_retry(error) is False, name


def test_a_silent_platform_does_not_disable_the_account(db):
    """Главное следствие: пять молчаний подряд кабинет не гасят.

    Именно это случилось 06.10 — и именно это больше не должно случаться.
    """
    account = make_account(db)

    for _ in range(FAILURE_THRESHOLD + 3):
        disabled = record_failure(db, account, str(TIMEOUT), transient=True)
        assert disabled is False
    db.commit()

    assert account.is_active is True, "кабинет погасили за молчание площадки"
    assert account.consecutive_failures == 0, (
        "счётчик вырос на отказе, который пройдёт сам — значит первый же "
        "настоящий отказ по ключу погасит кабинет мгновенно, не дав пяти попыток")


def test_the_reason_is_still_written_down(db):
    """Не гасим — но и не молчим: текст показывает «Диагностика» и скрипт."""
    account = make_account(db)

    record_failure(db, account, str(TIMEOUT), transient=True)
    db.commit()

    assert "Read timed out" in (account.last_error or ""), account.last_error


def test_a_broken_key_still_disables_after_five(db):
    """Обратная половина: ради неё предохранитель и существует."""
    account = make_account(db)

    for attempt in range(1, FAILURE_THRESHOLD + 1):
        disabled = record_failure(db, account, "401 Unauthorized")
        assert disabled is (attempt == FAILURE_THRESHOLD), attempt
    db.commit()

    assert account.is_active is False
    assert account.consecutive_failures == FAILURE_THRESHOLD


def test_an_outage_in_the_middle_does_not_reset_the_count(db):
    """Молчание между отказами по ключу счётчик не сбрасывает.

    Сбрасывает его только УСПЕХ (`record_success`). Иначе одна случайная
    сетевая икота посреди пяти 401 растягивала бы гашение навсегда, и кабинет
    с отозванным ключом долбил бы площадку месяцами.
    """
    account = make_account(db)

    for _ in range(4):
        record_failure(db, account, "401 Unauthorized")
    record_failure(db, account, str(TIMEOUT), transient=True)
    disabled = record_failure(db, account, "401 Unauthorized")
    db.commit()

    assert disabled is True, "пятый отказ по ключу обязан погасить"
    assert account.is_active is False


def test_the_poll_job_keeps_the_account_alive_on_a_timeout(db, monkeypatch):
    """То же самое, но БОЕВЫМ путём — через само задание опроса.

    Проверка по следствию: классификация может быть сколь угодно верной, а
    задание всё равно передаст `transient=False`, если забыть про аргумент, —
    и кабинет погаснет, как 06.10. Поэтому смотрим на состояние кабинета после
    настоящего прогона `job_poll_orders`.
    """
    account = make_account(db, platform=Platform.wb)
    account_id = account.id

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    class SilentPlatform:
        def get_orders_awaiting_confirmation(self):
            raise TIMEOUT

    monkeypatch.setattr(scheduler, "build_client",
                        lambda db_, account_id_: SilentPlatform())

    for _ in range(FAILURE_THRESHOLD + 2):
        scheduler.job_poll_orders(account_id)

    fresh = db.query(PlatformAccount).filter(
        PlatformAccount.id == account_id).first()
    assert fresh.is_active is True, (
        "задание опроса погасило кабинет за молчание площадки — "
        "ровно инцидент 06.10")

    # И молчания при этом нет: отметка неуспешная, причина записана. У ЖИВОГО
    # кабинета `/health` эту отметку не пропускает, то есть мониторинг покраснеет.
    hb = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == f"poll_orders_account_{account_id}").first()
    assert hb is not None and hb.last_success is False
    assert "Read timed out" in (hb.last_error or ""), hb.last_error
