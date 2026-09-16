import requests
import pytest

from app.workers.http_retry import with_retry


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _http_error(status_code, headers=None):
    resp = FakeResponse(status_code, headers)
    return requests.HTTPError(response=resp)


def test_succeeds_on_first_try():
    calls = []

    def func():
        calls.append(1)
        return "ok"

    result = with_retry(func)
    assert result == "ok"
    assert len(calls) == 1


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)  # не ждать по-настоящему в тесте

    calls = []

    def func():
        calls.append(1)
        if len(calls) < 2:
            raise _http_error(429, {"X-Ratelimit-Retry": "1"})
        return "ok"

    result = with_retry(func)
    assert result == "ok"
    assert len(calls) == 2


def test_raises_after_max_attempts_on_persistent_429(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    def func():
        raise _http_error(429)

    with pytest.raises(requests.HTTPError):
        with_retry(func, max_attempts=3)


def test_retries_on_5xx(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def func():
        calls.append(1)
        if len(calls) < 2:
            raise _http_error(503)
        return "ok"

    result = with_retry(func)
    assert result == "ok"
    assert len(calls) == 2


def test_does_not_retry_on_401():
    calls = []

    def func():
        calls.append(1)
        raise _http_error(401)

    with pytest.raises(requests.HTTPError):
        with_retry(func)

    assert len(calls) == 1  # ни одной повторной попытки — бессмысленно


def test_does_not_retry_on_400():
    calls = []

    def func():
        calls.append(1)
        raise _http_error(400)

    with pytest.raises(requests.HTTPError):
        with_retry(func)

    assert len(calls) == 1


def test_honors_wb_specific_retry_header_over_generic(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    calls = []

    def func():
        calls.append(1)
        if len(calls) < 2:
            raise _http_error(429, {"X-Ratelimit-Retry": "7", "Retry-After": "1"})
        return "ok"

    with_retry(func)
    assert sleep_calls[0] == 7.0  # WB-специфичный заголовок в приоритете


def test_retries_on_network_error(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def func():
        calls.append(1)
        if len(calls) < 2:
            raise requests.ConnectionError("boom")
        return "ok"

    result = with_retry(func)
    assert result == "ok"
    assert len(calls) == 2
