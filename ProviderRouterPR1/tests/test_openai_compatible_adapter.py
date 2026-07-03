from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nygen_router import ApiProtocol, ChatMessage, ProviderConfig, RouterRequest
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.errors import (
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderResponseError,
    ProviderTimeoutError,
)


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


def test_http_error_preserves_verbatim_message_and_structured_fields() -> None:
    body = {
        "error": {
            "message": "model gpt-4o-mini does not exist",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=body)

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderHTTPError) as exc_info:
        adapter.invoke(_request())

    error = exc_info.value
    # The provider's exact message is front-and-center, no unwrapping needed.
    assert error.message == "model gpt-4o-mini does not exist"
    assert "model gpt-4o-mini does not exist" in str(error)
    assert "HTTP 404 Not Found" in str(error)
    assert error.error_type == "invalid_request_error"
    assert error.error_code == "model_not_found"
    assert error.body == body
    assert error.status_code == 404
    # The standard httpx error stays reachable as the chained cause.
    assert isinstance(error.__cause__, httpx.HTTPStatusError)
    assert error.response is not None
    assert error.model == "model-a"


def test_timeout_raises_provider_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderTimeoutError) as exc_info:
        adapter.invoke(_request())

    error = exc_info.value
    assert error.provider_name == "provider_a"
    assert error.model == "model-a"
    assert "ReadTimeout" in str(error)
    assert isinstance(error.__cause__, httpx.ReadTimeout)
    assert error.original is error.__cause__


def test_connection_failure_raises_provider_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderConnectionError) as exc_info:
        adapter.invoke(_request())

    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)
    assert "ConnectError" in str(exc_info.value)


def test_other_transport_error_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("protocol error", request=request)

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as exc_info:
        adapter.invoke(_request())

    assert not isinstance(exc_info.value, (ProviderTimeoutError, ProviderConnectionError))
    assert isinstance(exc_info.value.__cause__, httpx.RemoteProtocolError)


def test_malformed_response_raises_provider_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_choices": []})

    adapter = OpenAICompatibleAdapter(_config(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderResponseError) as exc_info:
        adapter.invoke(_request())

    assert exc_info.value.provider_name == "provider_a"
    assert exc_info.value.model == "model-a"
