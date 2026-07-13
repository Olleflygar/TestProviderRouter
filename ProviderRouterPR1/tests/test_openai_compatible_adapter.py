from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest

from nygen_router import ApiProtocol, ProviderConfig
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.errors import (
    InvalidOperationArgumentsError,
    ProviderConnectionError,
    ProviderHTTPError,
    ProviderSDKNotInstalledError,
    ProviderTimeoutError,
    UnsupportedOperationError,
)


def _config() -> ProviderConfig:
    return ProviderConfig(
        name="provider_a",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://api.example.com/v1/",
        api_key="secret",
    )


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _arguments() -> dict[str, object]:
    return {"model": "model-a", "messages": [{"role": "user", "content": "Hello"}]}


def _completion_body(content: str = "Hi") -> dict[str, object]:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "model-a",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def test_adapter_sends_request_to_configured_endpoint() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_completion_body())

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    adapter.invoke("chat.completions.create", _arguments())

    assert captured["url"] == "https://api.example.com/v1/chat/completions"


def test_adapter_sends_arguments_and_authorization_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["payload"] = json.loads(request.content.decode())
        captured["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json=_completion_body())

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    adapter.invoke("chat.completions.create", _arguments())

    assert captured["payload"] == {
        "model": "model-a",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    assert captured["authorization"] == "Bearer secret"


def test_adapter_returns_raw_sdk_response_untouched() -> None:
    """The adapter must not parse/transform the response -- it's the real SDK object."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **_completion_body("Hello back"),
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    response = adapter.invoke("chat.completions.create", _arguments())

    assert type(response).__module__.startswith("openai.")
    assert response.choices[0].message.content == "Hello back"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 10
    assert response.usage.total_tokens == 30


def test_unsupported_operation_raises_for_bad_operation_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("no request should be sent")

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(UnsupportedOperationError) as exc_info:
        adapter.invoke("chat.completions.creat", _arguments())

    assert exc_info.value.provider_name == "provider_a"
    assert isinstance(exc_info.value.__cause__, AttributeError)
    assert exc_info.value.original is exc_info.value.__cause__


def test_invalid_operation_arguments_raises_for_bad_kwargs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("no request should be sent")

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(InvalidOperationArgumentsError) as exc_info:
        adapter.invoke("chat.completions.create", {"bogus_kwarg": 1})

    assert exc_info.value.provider_name == "provider_a"
    assert isinstance(exc_info.value.__cause__, TypeError)
    assert exc_info.value.original is exc_info.value.__cause__


def test_sdk_not_installed_raises_provider_sdk_not_installed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)
    adapter = OpenAICompatibleAdapter(_config())

    with pytest.raises(ProviderSDKNotInstalledError) as exc_info:
        adapter.invoke("chat.completions.create", _arguments())

    assert exc_info.value.provider_name == "provider_a"
    assert exc_info.value.package == "openai"
    assert isinstance(exc_info.value.original, ModuleNotFoundError)


@pytest.mark.parametrize("status_code", [401, 429])
def test_http_errors_raise_provider_http_error(status_code: int) -> None:
    """401 (auth) and 429 (rate limit) both map to the same ProviderHTTPError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"message": "provider rejected request"}},
        )

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(ProviderHTTPError) as exc_info:
        adapter.invoke("chat.completions.create", _arguments())

    assert exc_info.value.provider_name == "provider_a"
    assert exc_info.value.status_code == status_code


def test_http_error_preserves_verbatim_message_and_structured_fields() -> None:
    error_body = {
        "message": "model gpt-4o-mini does not exist",
        "type": "invalid_request_error",
        "code": "model_not_found",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": error_body})

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(ProviderHTTPError) as exc_info:
        adapter.invoke("chat.completions.create", _arguments())

    error = exc_info.value
    # The provider's exact message is front-and-center, no unwrapping needed.
    assert error.message == "model gpt-4o-mini does not exist"
    assert "model gpt-4o-mini does not exist" in str(error)
    assert "HTTP 404 Not Found" in str(error)
    assert error.error_type == "invalid_request_error"
    assert error.error_code == "model_not_found"
    assert error.body == error_body
    assert error.status_code == 404
    # The openai SDK's own status error stays reachable as the chained cause.
    assert isinstance(error.__cause__, Exception)
    assert type(error.__cause__).__name__ == "NotFoundError"
    assert error.response is not None
    assert error.model == "model-a"


def test_http_error_falls_back_to_synthesized_message_for_nonstandard_body() -> None:
    """When the body has no "message" key, fall back to the SDK's own summary string."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"foo": "bar"})

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(ProviderHTTPError) as exc_info:
        adapter.invoke("chat.completions.create", _arguments())

    assert "Error code: 500" in exc_info.value.message


def test_http_error_falls_back_to_synthesized_message_for_non_json_body() -> None:
    """A non-JSON error body parses to a plain string, not a dict -- fall back too."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"plain text error, not json")

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(ProviderHTTPError) as exc_info:
        adapter.invoke("chat.completions.create", _arguments())

    # exc.body is a plain str here (not a dict), so _verbatim_message falls back to
    # exc.message -- which the SDK sets to the raw body text for a non-JSON response.
    assert exc_info.value.message == "plain text error, not json"


def test_timeout_raises_provider_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(ProviderTimeoutError) as exc_info:
        adapter.invoke("chat.completions.create", _arguments())

    error = exc_info.value
    assert error.provider_name == "provider_a"
    assert error.model == "model-a"
    assert type(error.__cause__).__name__ == "APITimeoutError"
    assert error.original is error.__cause__


def test_connection_failure_raises_provider_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(ProviderConnectionError) as exc_info:
        adapter.invoke("chat.completions.create", _arguments())

    assert type(exc_info.value.__cause__).__name__ == "APIConnectionError"


def test_other_transport_error_also_raises_provider_connection_error() -> None:
    """The openai SDK folds every non-timeout transport failure into APIConnectionError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("protocol error", request=request)

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(ProviderConnectionError) as exc_info:
        adapter.invoke("chat.completions.create", _arguments())

    assert not isinstance(exc_info.value, ProviderTimeoutError)
    assert type(exc_info.value.__cause__).__name__ == "APIConnectionError"
