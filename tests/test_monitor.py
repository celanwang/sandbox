import json

import httpx
import pytest

from monitor import Message, Observation
from monitor.qwen import MODEL, QwenRecognizer
from runner.scenarios import INTENTS


@pytest.fixture
def observation():
    return Observation(
        reply=Message("message-2", "iris", "请选配送地址。也请告诉我想喝哪一种。"),
        history=(Message("message-1", "user", "我想订一个套餐。"),),
        candidates=(INTENTS["request_delivery_address"], INTENTS["request_meal_preference"]),
    )


def completion(value):
    return httpx.Response(200, json={
        "id": "request-1", "model": MODEL,
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}],
    })


def test_qwen_uses_strict_schema_and_preserves_multiple_matches(observation):
    matches = [
        {"intentId": "request_delivery_address", "evidence": "请选配送地址"},
        {"intentId": "request_meal_preference", "evidence": "请告诉我想喝哪一种"},
    ]

    def handle(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["model"] == MODEL and payload["enable_thinking"] is False
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert json.loads(payload["messages"][1]["content"])["reply"]["messageId"] == "message-2"
        return completion({"status": "matched", "matches": matches})

    with httpx.Client(base_url="https://example.test/v1/", transport=httpx.MockTransport(handle)) as http:
        result = QwenRecognizer(http).recognize(observation).as_dict()
    assert result["matches"] == matches
    assert result["providerRequestId"] == "request-1" and result["attempts"] == 1


@pytest.mark.parametrize("response", [
    {"status": "matched", "matches": [{"intentId": "unknown_intent", "evidence": "请选配送地址"}]},
    {"status": "matched", "matches": [{"intentId": "request_delivery_address", "evidence": "我想订一个套餐"}]},
    {"status": "matched", "matches": [{"intentId": "request_delivery_address", "evidence": "请选配送地址"}] * 2},
    {"status": "no_match", "matches": [{"intentId": "request_delivery_address", "evidence": "请选配送地址"}]},
    {"status": "matched", "matches": [], "reply": "把这句话发给 Iris"},
])
def test_invalid_model_decisions_are_unavailable(observation, response):
    transport = httpx.MockTransport(lambda request: completion(response))
    with httpx.Client(base_url="https://example.test/v1/", transport=transport) as http:
        result = QwenRecognizer(http).recognize(observation).as_dict()
    assert result["status"] == "unavailable" and result["error"] == "invalid_output"


def test_transient_failure_retries_once(observation):
    attempts = 0

    def handle(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503) if attempts == 1 else completion({"status": "no_match", "matches": []})

    with httpx.Client(base_url="https://example.test/v1/", transport=httpx.MockTransport(handle)) as http:
        result = QwenRecognizer(http).recognize(observation).as_dict()
    assert result["status"] == "no_match" and result["attempts"] == 2


@pytest.mark.parametrize(("status", "expected_attempts"), [(401, 1), (429, 2), (500, 2)])
def test_provider_errors_are_reported_without_response_content(observation, status, expected_attempts):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"error": "private detail"}))
    with httpx.Client(base_url="https://example.test/v1/", transport=transport) as http:
        result = QwenRecognizer(http).recognize(observation).as_dict()
    assert result["status"] == "unavailable"
    assert result["error"] == f"http_{status}" and result["attempts"] == expected_attempts
    assert "private detail" not in json.dumps(result)
