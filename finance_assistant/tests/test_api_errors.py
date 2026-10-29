"""Ошибки Claude API, которые не лечатся повтором, — понятным текстом, что делать."""
import anthropic
import httpx2
import pytest

from finance.recognize import ClaudeRecognizer, RecognitionError


def recognizer(code, message, kind):
    def handler(request):
        return httpx2.Response(code, json={"type": "error", "error": {"type": kind, "message": message}})
    r = ClaudeRecognizer.__new__(ClaudeRecognizer)
    r.model = "claude-opus-5"
    r.client = anthropic.Anthropic(
        api_key="sk-ant-x", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return r


@pytest.mark.parametrize("code,message,kind,expected", [
    (401, "invalid x-api-key", "authentication_error", "ключ Claude API не принят"),
    (403, "forbidden", "permission_error", "нет доступа"),
    (400, "Your credit balance is too low to access the Anthropic API.",
     "invalid_request_error", "закончились деньги"),
])
def test_api_errors_explain_what_to_do(code, message, kind, expected):
    r = recognizer(code, message, kind)
    with pytest.raises(RecognitionError, match=expected):
        r.recognize_payment([], "100 такси", today="2026-10-29", cards=[], categories=["Прочее"])
