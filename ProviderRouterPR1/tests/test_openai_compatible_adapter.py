from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nygen_router import ApiProtocol, ChatMessage, ProviderConfig, RouterRequest
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.errors import ProviderHTTPError, ProviderResponseError


def _config() -> ProviderConfig:
    return ProviderConfig(
        name="provider_a",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://api.example.com/v1/",
        api_key="secret",
    )


def _request() -> RouterRequest:
    return RouterRequest(messages=[ChatMessage(role="user", content="Hello")])


def test_adapter_sends_request_to_chat_completions() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Hi"}}]},
        )

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    adapter.invoke(_request())

    assert captured["url"] == "https://api.example.com/v1/chat/completions"


def test_adapter_includes_model_and_messages() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Hi"}}]},
        )

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    adapter.invoke(_request())

    assert captured["payload"] == {
        "model": "model-a",
        "messages": [{"role": "user", "content": "Hello"}],
    }


def test_adapter_includes_authorization_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["content_type"] = request.headers["Content-Type"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Hi"}}]},
        )

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    adapter.invoke(_request())

    assert captured["authorization"] == "Bearer secret"
    assert captured["content_type"] == "application/json"


def test_adapter_parses_response_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Hello back"}}]},
        )

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    response = adapter.invoke(_request())

    assert response.text == "Hello back"
    assert response.provider_name == "provider_a"
    assert response.model == "model-a"


def test_adapter_allows_empty_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}}]},
        )

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    response = adapter.invoke(_request())

    assert response.text == ""


def test_adapter_rejects_null_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None}}]},
        )

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderResponseError):
        adapter.invoke(_request())


def test_adapter_parses_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello back"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            },
        )

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    response = adapter.invoke(_request())

    assert response.usage is not None
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 20
    assert response.usage.total_tokens == 30


@pytest.mark.parametrize("status_code", [401, 429])
def test_http_errors_raise_provider_http_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"message": "provider rejected request"}},
        )

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderHTTPError) as exc_info:
        adapter.invoke(_request())

    assert exc_info.value.provider_name == "provider_a"
    assert exc_info.value.status_code == status_code


def test_malformed_response_raises_provider_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_choices": []})

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderResponseError):
        adapter.invoke(_request())
