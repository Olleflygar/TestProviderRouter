from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from nygen_router import (
    ApiProtocol,
    InvalidOperationArgumentsError,
    NormalizedStream,
    ProviderConfig,
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderResponsesError,
    ProviderSDKNotInstalledError,
    ProviderTimeoutError,
    UnsupportedOperationError,
)
from nygen_router.adapters.openai_responses import OpenAIResponsesAdapter
from nygen_router.errors import ErrorCategory, categorize_error


def _config(*, timeout_seconds: float = 12.5) -> ProviderConfig:
    return ProviderConfig(
        name="responses_provider",
        protocol=ApiProtocol.OPENAI_RESPONSES,
        model="model-r",
        base_url="https://responses.example.com/v1/",
        api_key="test-api-key",
        timeout_seconds=timeout_seconds,
    )


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _usage() -> dict[str, object]:
    return {
        "input_tokens": 3,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 4,
        "output_tokens_details": {"reasoning_tokens": 1},
        "total_tokens": 7,
    }


def _response_body(
    *,
    status: str = "completed",
    text: str = "Hello from Responses",
    response_id: str = "resp_123",
    reason: str | None = None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 0.0,
        "status": status,
        "model": "model-r",
        "output": [
            {
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tools": [],
        "tool_choice": "auto",
        "usage": _usage(),
        "incomplete_details": None if reason is None else {"reason": reason},
        "error": error,
    }


def _arguments(**extra: object) -> dict[str, object]:
    return {"model": "model-r", "input": "Hello", **extra}


def _adapter(handler: Any) -> OpenAIResponsesAdapter:
    return OpenAIResponsesAdapter(_config(), http_client=_client(handler))


def test_responses_create_uses_native_endpoint_client_configuration_and_no_sdk_retries() -> None:
    captured: dict[str, Any] = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(500, json={"error": {"message": "one attempt only"}})

    adapter = _adapter(handler)

    with pytest.raises(ProviderHTTPError):
        adapter.invoke("responses.create", _arguments())

    assert captured["calls"] == 1
    assert captured["url"] == "https://responses.example.com/v1/responses"
    assert captured["authorization"] == "Bearer test-api-key"
    assert set(captured["timeout"].values()) == {12.5}


def test_native_arguments_and_router_model_pass_through_without_translation() -> None:
    captured: dict[str, object] = {}
    arguments = _arguments(
        instructions="Return JSON",
        tools=[
            {
                "type": "function",
                "name": "lookup",
                "description": "Look something up",
                "parameters": {"type": "object", "properties": {"term": {"type": "string"}}},
                "strict": True,
            }
        ],
        text={"format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}}},
        reasoning={"effort": "medium"},
        previous_response_id="resp_previous",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode()))
        return httpx.Response(200, json=_response_body())

    _adapter(handler).invoke("responses.create", arguments)

    assert captured == arguments
    assert "messages" not in captured


def test_completed_response_is_the_native_sdk_object_with_output_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response_body())

    response = _adapter(handler).invoke("responses.create", _arguments())

    assert type(response).__module__.startswith("openai.types.responses")
    assert response.output_text == "Hello from Responses"
    assert response.output[0].type == "message"
    assert response.usage.total_tokens == 7
    assert response.status == "completed"


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_background_nonterminal_response_is_returned_natively(status: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response_body(status=status))

    response = _adapter(handler).invoke("responses.create", _arguments(background=True))

    assert response.status == status
    assert type(response).__module__.startswith("openai.types.responses")


def test_nonstreaming_incomplete_response_warns_once_and_returns_native_object(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_response_body(status="incomplete", reason="max_output_tokens"),
        )

    with caplog.at_level(logging.WARNING):
        response = _adapter(handler).invoke("responses.create", _arguments())

    assert response.status == "incomplete"
    assert response.incomplete_details.reason == "max_output_tokens"
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "responses_provider" in warnings[0]
    assert "model-r" in warnings[0]
    assert "resp_123" in warnings[0]
    assert "max_output_tokens" in warnings[0]
    assert "test-api-key" not in warnings[0]
    assert "Hello" not in warnings[0]


def test_nonstreaming_failed_response_preserves_native_details() -> None:
    body = _response_body(
        status="failed",
        error={"code": "server_error", "message": "generation backend failed"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderResponsesError) as exc_info:
        _adapter(handler).invoke("responses.create", _arguments())

    error = exc_info.value
    assert error.message == "generation backend failed"
    assert error.error_code == "server_error"
    assert error.event is None
    assert error.response.id == "resp_123"
    assert error.original is None
    assert error.__cause__ is None
    assert categorize_error(error) is ErrorCategory.SERVER_ERROR


def test_unsupported_operation_preserves_attribute_error_cause() -> None:
    adapter = _adapter(lambda request: pytest.fail("request should not be sent"))

    with pytest.raises(UnsupportedOperationError) as exc_info:
        adapter.invoke("responses.creat", _arguments())

    assert isinstance(exc_info.value.__cause__, AttributeError)
    assert exc_info.value.original is exc_info.value.__cause__


def test_invalid_arguments_preserve_type_error_cause() -> None:
    adapter = _adapter(lambda request: pytest.fail("request should not be sent"))

    with pytest.raises(InvalidOperationArgumentsError) as exc_info:
        adapter.invoke("responses.create", {"model": "model-r", "not_a_real_argument": True})

    assert isinstance(exc_info.value.__cause__, TypeError)
    assert exc_info.value.original is exc_info.value.__cause__


def test_missing_optional_sdk_uses_existing_transparent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)

    with pytest.raises(ProviderSDKNotInstalledError) as exc_info:
        OpenAIResponsesAdapter(_config()).invoke("responses.create", _arguments())

    assert exc_info.value.provider_name == "responses_provider"
    assert isinstance(exc_info.value.original, ModuleNotFoundError)


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422, 429, 500, 503])
def test_http_status_mapping_preserves_verbatim_structured_error(status_code: int) -> None:
    provider_body = {
        "message": f"provider message {status_code}",
        "type": "invalid_request_error",
        "code": f"code_{status_code}",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": provider_body})

    with pytest.raises(ProviderHTTPError) as exc_info:
        _adapter(handler).invoke("responses.create", _arguments())

    error = exc_info.value
    assert error.status_code == status_code
    assert error.message == f"provider message {status_code}"
    assert error.error_type == "invalid_request_error"
    assert error.error_code == f"code_{status_code}"
    assert error.body == provider_body
    assert error.original is error.__cause__


def test_non_json_error_body_is_preserved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"plain upstream failure")

    with pytest.raises(ProviderHTTPError) as exc_info:
        _adapter(handler).invoke("responses.create", _arguments())

    assert exc_info.value.message == "plain upstream failure"


@pytest.mark.parametrize(
    ("transport_error", "router_error"),
    [
        (httpx.ReadTimeout("read timed out"), ProviderTimeoutError),
        (httpx.ConnectError("connection refused"), ProviderConnectionError),
    ],
)
def test_transport_errors_map_through_shared_openai_path(
    transport_error: Exception, router_error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise transport_error

    with pytest.raises(router_error) as exc_info:
        _adapter(handler).invoke("responses.create", _arguments())

    assert exc_info.value.original is exc_info.value.__cause__


def _sse(*events: dict[str, object]) -> Iterator[bytes]:
    for event in events:
        yield f"data: {json.dumps(event)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def _stream(
    body: Iterator[bytes] | bytes | httpx.SyncByteStream,
) -> NormalizedStream:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, httpx.SyncByteStream):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=body,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    result = _adapter(handler).invoke("responses.create", _arguments(stream=True))
    assert isinstance(result, NormalizedStream)
    return result


def _event(event_type: str, sequence_number: int, **fields: object) -> dict[str, object]:
    return {"type": event_type, "sequence_number": sequence_number, **fields}


def test_stream_yields_native_typed_events_unchanged_and_observes_completion() -> None:
    completed = _response_body()
    stream = _stream(
        _sse(
            _event(
                "response.created",
                0,
                response=_response_body(status="in_progress"),
            ),
            _event(
                "response.output_text.delta",
                1,
                item_id="msg_123",
                output_index=0,
                content_index=0,
                delta="Hello",
                logprobs=[],
            ),
            _event("response.completed", 2, response=completed),
        )
    )

    events = list(stream)

    assert [event.type for event in events] == [
        "response.created",
        "response.output_text.delta",
        "response.completed",
    ]
    assert [type(event).__module__ for event in events] == [
        "openai.types.responses.response_created_event",
        "openai.types.responses.response_text_delta_event",
        "openai.types.responses.response_completed_event",
    ]
    assert stream.completed is True
    assert stream.recognized is True
    assert stream.usage.total_tokens == 7


def test_stream_incomplete_is_terminal_warns_once_and_preserves_future_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = _stream(
        _sse(
            _event(
                "response.output_text.delta",
                0,
                item_id="msg_123",
                output_index=0,
                content_index=0,
                delta="partial",
                logprobs=[],
            ),
            _event(
                "response.incomplete",
                1,
                response=_response_body(status="incomplete", reason="future_limit"),
            ),
        )
    )

    with caplog.at_level(logging.WARNING):
        events = list(stream)

    assert events[-1].type == "response.incomplete"
    assert stream.completed is True
    assert stream.usage.total_tokens == 7
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "future_limit" in warnings[0]


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("rate_limit_exceeded", ErrorCategory.RATE_LIMIT),
        ("server_error", ErrorCategory.SERVER_ERROR),
        ("vector_store_timeout", ErrorCategory.TIMEOUT),
        ("invalid_prompt", ErrorCategory.BAD_REQUEST),
        ("future_provider_code", ErrorCategory.UNKNOWN),
    ],
)
def test_stream_error_event_preserves_typed_event_and_categorizes_known_codes(
    code: str, category: ErrorCategory
) -> None:
    stream = _stream(
        _sse(
            _event(
                "error",
                0,
                code=code,
                message="native event message",
                param="input",
            )
        )
    )

    with pytest.raises(ProviderResponsesError) as exc_info:
        next(stream)

    error = exc_info.value
    assert error.event.type == "error"
    assert error.error_code == code
    assert error.message == "native event message"
    assert error.param == "input"
    assert error.response is None
    assert error.original is None
    assert error.__cause__ is None
    assert categorize_error(error) is category


def test_response_failed_event_preserves_embedded_response() -> None:
    failed = _response_body(
        status="failed",
        error={"code": "server_error", "message": "embedded failure"},
    )
    stream = _stream(_sse(_event("response.failed", 0, response=failed)))

    with pytest.raises(ProviderResponsesError) as exc_info:
        next(stream)

    assert exc_info.value.event.type == "response.failed"
    assert exc_info.value.response.id == "resp_123"
    assert exc_info.value.message == "embedded failure"


def test_recognized_nonterminal_stream_ends_without_completion_marker() -> None:
    stream = _stream(
        _sse(
            _event(
                "response.output_text.delta",
                0,
                item_id="msg_123",
                output_index=0,
                content_index=0,
                delta="partial",
                logprobs=[],
            )
        )
    )

    list(stream)

    assert stream.recognized is True
    assert stream.completed is False


def test_genuinely_unfamiliar_stream_shape_remains_unrecognized() -> None:
    stream = _stream(_sse({"type": "vendor.future_event", "payload": "opaque"}))

    events = list(stream)

    assert len(events) == 1
    assert events[0].type == "vendor.future_event"
    assert stream.recognized is False
    assert stream.completed is False


def _dying_body(exc: Exception) -> Iterator[bytes]:
    event = _event(
        "response.output_text.delta",
        0,
        item_id="msg_123",
        output_index=0,
        content_index=0,
        delta="partial",
        logprobs=[],
    )
    yield f"data: {json.dumps(event)}\n\n".encode()
    raise exc


@pytest.mark.parametrize(
    ("transport_error", "router_error"),
    [
        (httpx.ReadTimeout("read timed out"), ProviderTimeoutError),
        (httpx.RemoteProtocolError("peer closed"), ProviderConnectionError),
    ],
)
def test_midstream_transport_error_never_leaks_foreign_exception(
    transport_error: Exception, router_error: type[Exception]
) -> None:
    stream = _stream(_dying_body(transport_error))

    next(stream)
    with pytest.raises(router_error) as exc_info:
        next(stream)

    assert exc_info.value.original is exc_info.value.__cause__
    assert exc_info.value.__cause__ is transport_error


def test_malformed_sse_payload_leaves_as_router_error() -> None:
    good = _event(
        "response.output_text.delta",
        0,
        item_id="msg_123",
        output_index=0,
        content_index=0,
        delta="partial",
        logprobs=[],
    )
    stream = _stream(f"data: {json.dumps(good)}\n\ndata: {{bad json\n\n".encode())

    next(stream)
    with pytest.raises(ProviderError) as exc_info:
        next(stream)

    assert type(exc_info.value).__module__.startswith("nygen_router.")
    assert isinstance(exc_info.value.__cause__, ValueError)


class _ClosableBody(httpx.SyncByteStream):
    def __init__(self, *events: dict[str, object]) -> None:
        self._events = events
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from _sse(*self._events)

    def close(self) -> None:
        self.closed = True


def test_close_propagates_to_underlying_sdk_response() -> None:
    body = _ClosableBody(
        _event(
            "response.output_text.delta",
            0,
            item_id="msg_123",
            output_index=0,
            content_index=0,
            delta="partial",
            logprobs=[],
        )
    )
    stream = _stream(body)

    next(stream)
    assert body.closed is False
    stream.close()

    assert body.closed is True
