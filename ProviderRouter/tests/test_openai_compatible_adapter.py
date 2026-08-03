from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from nygen_router import ApiProtocol, NormalizedStream, ProviderConfig
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.errors import (
    InvalidOperationArgumentsError,
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderSDKNotInstalledError,
    ProviderTimeoutError,
    UnsupportedOperationError,
)


def _config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="provider_a",
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

    assert exc_info.value.provider_id == "provider_a"
    assert exc_info.value.provider_name == "provider_a"
    assert isinstance(exc_info.value.__cause__, AttributeError)
    assert exc_info.value.original is exc_info.value.__cause__


def test_invalid_operation_arguments_raises_for_bad_kwargs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("no request should be sent")

    adapter = OpenAICompatibleAdapter(_config(), http_client=_client(handler))

    with pytest.raises(InvalidOperationArgumentsError) as exc_info:
        adapter.invoke("chat.completions.create", {"bogus_kwarg": 1})

    assert exc_info.value.provider_id == "provider_a"
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

    assert exc_info.value.provider_id == "provider_a"
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

    assert exc_info.value.provider_id == "provider_a"
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
    assert error.provider_id == "provider_a"
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
    assert error.provider_id == "provider_a"
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

    assert exc_info.value.provider_id == "provider_a"
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


def _chunk_json(content: str = "hi", finish_reason: str | None = None) -> str:
    return json.dumps(
        {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "model-a",
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": finish_reason}
            ],
        }
    )


def _usage_chunk_json() -> str:
    """The include_usage final chunk: usage, no choices, after the finish_reason chunk."""
    return json.dumps(
        {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "model-a",
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }
    )


def _sse(*payloads: str) -> Iterator[bytes]:
    for payload in payloads:
        yield f"data: {payload}\n\n".encode()


def _stream_client(body: Iterator[bytes] | bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _stream_arguments() -> dict[str, object]:
    return {**_arguments(), "stream": True}


def _stream(body: Iterator[bytes] | bytes) -> Any:
    adapter = OpenAICompatibleAdapter(_config(), http_client=_stream_client(body))
    return adapter.invoke("chat.completions.create", _stream_arguments())


def test_streaming_response_is_wrapped_in_a_normalized_stream() -> None:
    """Wrapping is the opt-in that lets the router observe a stream it must not interpret."""
    stream = _stream(_sse(_chunk_json(finish_reason="stop"), "[DONE]"))

    assert isinstance(stream, NormalizedStream)


def test_wrapped_stream_yields_the_sdk_chunks_unchanged_and_marks_completion() -> None:
    stream = _stream(_sse(_chunk_json("one"), _chunk_json("two", finish_reason="stop"), "[DONE]"))

    chunks = list(stream)

    assert [type(chunk).__name__ for chunk in chunks] == [
        "ChatCompletionChunk",
        "ChatCompletionChunk",
    ]
    assert [chunk.choices[0].delta.content for chunk in chunks] == ["one", "two"]
    assert stream.completed is True
    assert stream.recognized is True


def test_wrapped_stream_pockets_usage_without_disturbing_completion() -> None:
    """The usage chunk has no choices and arrives last; it must not undo the marker."""
    stream = _stream(
        _sse(
            _chunk_json("one", finish_reason="stop"),
            _usage_chunk_json(),
            "[DONE]",
        )
    )

    list(stream)

    assert stream.usage is not None
    assert stream.usage.total_tokens == 7
    assert stream.completed is True
    assert stream.recognized is True


def test_wrapped_stream_without_finish_reason_is_not_completed() -> None:
    stream = _stream(_sse(_chunk_json("one"), "[DONE]"))

    list(stream)

    assert stream.completed is False
    assert stream.recognized is True  # the shape was readable; the marker never came


def test_wrapped_stream_of_an_unfamiliar_shape_is_not_recognized() -> None:
    """A chunk carrying no choices at all tells the wrapper nothing about completion."""
    stream = _stream(_sse(json.dumps({"id": "x", "object": "other", "created": 0}), "[DONE]"))

    list(stream)

    assert stream.recognized is False
    assert stream.completed is False


def _dying_body(exc: Exception) -> Iterator[bytes]:
    yield f"data: {_chunk_json('one')}\n\n".encode()
    raise exc


def test_mid_stream_timeout_raises_provider_timeout_error() -> None:
    """During SSE iteration the SDK does not fold transport errors into its own types."""
    stream = _stream(_dying_body(httpx.ReadTimeout("read timed out")))

    assert next(stream).choices[0].delta.content == "one"
    with pytest.raises(ProviderTimeoutError) as exc_info:
        next(stream)

    assert exc_info.value.provider_id == "provider_a"
    assert isinstance(exc_info.value.__cause__, httpx.ReadTimeout)
    assert exc_info.value.original is exc_info.value.__cause__


def test_mid_stream_transport_error_raises_provider_connection_error() -> None:
    stream = _stream(_dying_body(httpx.RemoteProtocolError("peer closed connection")))

    next(stream)
    with pytest.raises(ProviderConnectionError) as exc_info:
        next(stream)

    assert exc_info.value.provider_id == "provider_a"
    assert not isinstance(exc_info.value, ProviderTimeoutError)
    assert isinstance(exc_info.value.__cause__, httpx.RemoteProtocolError)


def test_mid_stream_sse_error_event_raises_provider_error() -> None:
    body = (
        f"data: {_chunk_json('one')}\n\n".encode()
        + b'event: error\ndata: {"error":{"message":"upstream exploded"}}\n\n'
    )
    stream = _stream(body)

    next(stream)
    with pytest.raises(ProviderError) as exc_info:
        next(stream)

    assert exc_info.value.provider_id == "provider_a"
    assert "upstream exploded" in str(exc_info.value)


def test_mid_stream_malformed_payload_still_leaves_as_a_router_error() -> None:
    """Only router errors leave __next__, whatever the provider sent down the wire."""
    body = f"data: {_chunk_json('one')}\n\n".encode() + b"data: {not json\n\n"
    stream = _stream(body)

    next(stream)
    with pytest.raises(ProviderError) as exc_info:
        next(stream)

    assert isinstance(exc_info.value.__cause__, ValueError)  # JSONDecodeError
    assert "JSONDecodeError" in str(exc_info.value)


class _ClosableBody(httpx.SyncByteStream):
    """Response body that records whether httpx was asked to close it."""

    def __init__(self, *payloads: str) -> None:
        self._payloads = payloads
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for payload in self._payloads:
            yield f"data: {payload}\n\n".encode()

    def close(self) -> None:
        self.closed = True


def test_closing_the_wrapper_releases_the_underlying_response() -> None:
    """close() has to reach all the way down, or an abandoned stream leaks its connection."""
    body = _ClosableBody(_chunk_json("one"), _chunk_json("two"), "[DONE]")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    adapter = OpenAICompatibleAdapter(
        _config(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    stream = adapter.invoke("chat.completions.create", _stream_arguments())

    next(stream)
    assert body.closed is False
    stream.close()

    assert body.closed is True
