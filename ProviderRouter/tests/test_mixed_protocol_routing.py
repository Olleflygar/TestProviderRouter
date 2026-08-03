from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    InvalidOperationArgumentsError,
    MetricsEvent,
    NormalizedStream,
    ProviderConfig,
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderResponsesError,
    ProviderRouter,
    ProviderStreamInterruptedError,
    ProviderTimeoutError,
    RouterExhaustedError,
    RoutingContext,
    StreamFailurePolicy,
    UnsupportedOperationError,
    UnsupportedProtocolError,
)
from nygen_router.adapters.openai_responses import OpenAIResponsesAdapter


class _StaticPolicy:
    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(eligible)


class _Store:
    def __init__(self) -> None:
        self.events: list[MetricsEvent] = []

    def record_attempt(self, event: MetricsEvent) -> None:
        self.events.append(event)

    def query_recent(
        self,
        *,
        since: datetime,
        metrics_scope: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        protocol: ApiProtocol | None = None,
        call_type: CallType | None = None,
    ) -> list[MetricsEvent]:
        return list(self.events)


class _FakeStream(NormalizedStream):
    def __init__(
        self,
        chunks: list[Any],
        *,
        completed_at: int | None = None,
        error: Exception | None = None,
        recognized: bool = True,
    ) -> None:
        self._chunks = list(chunks)
        self._completed_at = completed_at
        self._error = error
        self._recognized = recognized
        self._index = 0
        self._completed = False
        self.close_calls = 0

    def __next__(self) -> Any:
        if self._index >= len(self._chunks):
            if self._error is not None:
                raise self._error
            raise StopIteration
        chunk = self._chunks[self._index]
        if self._index == self._completed_at:
            self._completed = True
        self._index += 1
        return chunk

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def recognized(self) -> bool:
        return self._recognized

    @property
    def usage(self) -> Any:
        return None

    def close(self) -> None:
        self.close_calls += 1


class _Script:
    def __init__(self, behaviors: dict[str, list[Any]]) -> None:
        self.behaviors = {name: list(queue) for name, queue in behaviors.items()}
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def invoke(self, config: ProviderConfig, operation: str, arguments: dict[str, object]) -> Any:
        self.calls.append((config.name, operation, arguments))
        queue = self.behaviors.get(config.name)
        if not queue:
            raise AssertionError(f"No behavior scripted for {config.name!r}")
        behavior = queue.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _Adapter:
    def __init__(self, config: ProviderConfig, script: _Script) -> None:
        self.config = config
        self._script = script

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        return self._script.invoke(self.config, operation, arguments)


def _config(name: str, protocol: ApiProtocol, model: str) -> ProviderConfig:
    return ProviderConfig(
        provider_id=name,
        name=name,
        protocol=protocol,
        model=model,
        base_url=f"https://{name}.example.com/v1/",
        api_key="secret",
    )


def _calls(*, stream: bool = False) -> list[CallVariant]:
    return [
        CallVariant(
            call_type=CallType.STREAMING if stream else CallType.REGULAR,
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={
                "messages": [{"role": "user", "content": "chat input"}],
                **({"stream": True} if stream else {}),
            },
        ),
        CallVariant(
            call_type=CallType.STREAMING if stream else CallType.REGULAR,
            protocol=ApiProtocol.OPENAI_RESPONSES,
            operation="responses.create",
            arguments={
                "input": "responses input",
                **({"stream": True} if stream else {}),
            },
        ),
    ]


def _router(
    providers: list[ProviderConfig],
    script: _Script,
    *,
    stream_failure_policy: StreamFailurePolicy = StreamFailurePolicy.RESTART,
) -> tuple[ProviderRouter, _Store]:
    store = _Store()

    def factory(config: ProviderConfig) -> _Adapter:
        return _Adapter(config, script)

    return (
        ProviderRouter(
            providers,
            metrics_scope="test",
            adapter_factory=factory,
            policy=_StaticPolicy(),
            metrics_store=store,  # type: ignore[arg-type]
            stream_failure_policy=stream_failure_policy,
        ),
        store,
    )


def _timeout(provider: ProviderConfig) -> ProviderTimeoutError:
    return ProviderTimeoutError(
        f"{provider.name} timed out",
        provider_id=provider.name,
        provider_name=provider.name,
        model=provider.model,
    )


def _bad_request(provider: ProviderConfig) -> Exception:
    if provider.protocol is ApiProtocol.OPENAI_RESPONSES:
        return ProviderResponsesError(
            provider_id=provider.name,
            provider_name=provider.name,
            model=provider.model,
            message="invalid native input",
            error_code="invalid_prompt",
            param="input",
        )
    return ProviderHTTPError(
        provider_id=provider.name,
        provider_name=provider.name,
        model=provider.model,
        status_code=400,
        message="invalid chat messages",
    )


def _invalid_operation(provider: ProviderConfig) -> UnsupportedOperationError:
    return UnsupportedOperationError(
        "invalid operation",
        provider_id=provider.name,
        provider_name=provider.name,
        model=provider.model,
    )


def test_responses_is_supported_by_the_default_registry_and_adapter() -> None:
    provider = _config("responses", ApiProtocol.OPENAI_RESPONSES, "responses-model")
    router = ProviderRouter([provider], metrics_scope="test", metrics_store=None)
    invalid_call = CallVariant(
        call_type=CallType.REGULAR,
        protocol=ApiProtocol.OPENAI_RESPONSES,
        operation="responses.creat",
        arguments={"input": "never sent"},
    )

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke([invalid_call])

    assert len(exc_info.value.attempts) == 1
    assert isinstance(exc_info.value.attempts[0].error, UnsupportedOperationError)
    assert isinstance(exc_info.value.attempts[0].error.__cause__, AttributeError)


def test_unsupported_protocol_error_names_both_builtin_protocols() -> None:
    error = UnsupportedProtocolError("custom-id", "custom", ApiProtocol.ANTHROPIC_MESSAGES)

    assert "'openai_chat'" in str(error)
    assert "'openai_responses'" in str(error)
    assert 'id="custom-id"' in str(error)


def test_responses_variant_receives_model_copy_without_mutating_original() -> None:
    provider = _config("responses", ApiProtocol.OPENAI_RESPONSES, "responses-model")
    response = object()
    script = _Script({"responses": [response]})
    router, store = _router([provider], script)
    variant = _calls()[1]

    assert router.invoke([variant]) is response

    assert script.calls == [
        (
            "responses",
            "responses.create",
            {"input": "responses input", "model": "responses-model"},
        )
    ]
    assert variant.arguments == {"input": "responses input"}
    assert store.events[0].protocol is ApiProtocol.OPENAI_RESPONSES


@pytest.mark.parametrize(
    ("first_protocol", "second_protocol"),
    [
        (ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES),
        (ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT),
    ],
)
def test_retryable_failure_falls_back_across_protocols_with_each_native_variant(
    first_protocol: ApiProtocol, second_protocol: ApiProtocol
) -> None:
    first = _config("first", first_protocol, "first-model")
    second = _config("second", second_protocol, "second-model")
    script = _Script({"first": [_timeout(first)], "second": ["served"]})
    router, store = _router([first, second], script)

    assert router.invoke(_calls()) == "served"

    assert [call[0] for call in script.calls] == ["first", "second"]
    expected_operations = {
        ApiProtocol.OPENAI_CHAT: "chat.completions.create",
        ApiProtocol.OPENAI_RESPONSES: "responses.create",
    }
    assert script.calls[0][1] == expected_operations[first_protocol]
    assert script.calls[1][1] == expected_operations[second_protocol]
    assert script.calls[0][2]["model"] == "first-model"
    assert script.calls[1][2]["model"] == "second-model"
    assert ("messages" in script.calls[0][2]) is (first_protocol is ApiProtocol.OPENAI_CHAT)
    assert ("input" in script.calls[1][2]) is (second_protocol is ApiProtocol.OPENAI_RESPONSES)
    assert [event.success for event in store.events] == [False, True]


@pytest.mark.parametrize("failure_kind", ["connection", "rate_limit", "auth", "server", "unknown"])
@pytest.mark.parametrize(
    ("first_protocol", "second_protocol"),
    [
        (ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES),
        (ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT),
    ],
)
def test_every_retryable_category_continues_across_protocols(
    failure_kind: str,
    first_protocol: ApiProtocol,
    second_protocol: ApiProtocol,
) -> None:
    first = _config("first", first_protocol, "first-model")
    second = _config("second", second_protocol, "second-model")
    if failure_kind == "connection":
        failure: Exception = ProviderConnectionError(
            "connection failed",
            provider_id=first.name,
            provider_name=first.name,
            model=first.model,
        )
    elif failure_kind == "rate_limit":
        failure = ProviderHTTPError(
            provider_id=first.name,
            provider_name=first.name,
            model=first.model,
            status_code=429,
            message="rate limited",
        )
    elif failure_kind == "auth":
        failure = ProviderHTTPError(
            provider_id=first.name,
            provider_name=first.name,
            model=first.model,
            status_code=401,
            message="invalid credential",
        )
    elif failure_kind == "server":
        failure = ProviderHTTPError(
            provider_id=first.name,
            provider_name=first.name,
            model=first.model,
            status_code=503,
            message="temporarily unavailable",
        )
    else:
        failure = ProviderError(
            "future provider failure",
            provider_id=first.name,
            provider_name=first.name,
            model=first.model,
        )
    script = _Script({"first": [failure], "second": ["served"]})
    router, store = _router([first, second], script)

    assert router.invoke(_calls()) == "served"

    assert [call[0] for call in script.calls] == ["first", "second"]
    assert [event.success for event in store.events] == [False, True]


def test_declared_responses_rate_limit_falls_back_and_benches_provider() -> None:
    first = _config("first", ApiProtocol.OPENAI_RESPONSES, "responses-model")
    second = _config("second", ApiProtocol.OPENAI_CHAT, "chat-model")
    failure = ProviderResponsesError(
        provider_id=first.name,
        provider_name=first.name,
        model=first.model,
        message="provider flow control",
        error_code="rate_limit_exceeded",
    )
    script = _Script({"first": [failure], "second": ["served"]})
    router, store = _router([first, second], script)

    assert router.invoke(_calls()) == "served"

    assert [call[0] for call in script.calls] == ["first", "second"]
    assert [event.success for event in store.events] == [False, True]
    assert router.health_report()["first"].cooldown_remaining_seconds > 0


@pytest.mark.parametrize("error_factory", [_bad_request, _invalid_operation])
@pytest.mark.parametrize(
    ("first_protocol", "second_protocol"),
    [
        (ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES),
        (ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT),
    ],
)
def test_nonstreaming_stop_categories_remain_global_and_do_not_touch_health(
    error_factory: Any,
    first_protocol: ApiProtocol,
    second_protocol: ApiProtocol,
) -> None:
    first = _config("first", first_protocol, "first-model")
    second = _config("second", second_protocol, "second-model")
    failure = error_factory(first)
    script = _Script({"first": [failure], "second": ["must not run"]})
    router, store = _router([first, second], script)

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke(_calls())

    assert [call[0] for call in script.calls] == ["first"]
    assert exc_info.value.attempts[0].error is failure
    assert len(store.events) == 1
    assert store.events[0].success is False
    assert router.health_report()["first"].consecutive_failures == 0


@pytest.mark.parametrize(
    ("first_protocol", "second_protocol"),
    [
        (ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES),
        (ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT),
    ],
)
def test_active_stream_stop_category_does_not_restart_across_protocols(
    first_protocol: ApiProtocol, second_protocol: ApiProtocol
) -> None:
    first = _config("first", first_protocol, "first-model")
    second = _config("second", second_protocol, "second-model")
    failure = _bad_request(first)
    stream = _FakeStream(["partial"], error=failure)
    script = _Script({"first": [stream], "second": [_FakeStream(["must not run"])]})
    router, store = _router([first, second], script)
    routed = router.invoke(_calls(stream=True))

    assert next(routed) == "partial"
    with pytest.raises(type(failure)) as exc_info:
        next(routed)

    assert exc_info.value is failure
    assert [call[0] for call in script.calls] == ["first"]
    assert store.events[0].stream_opened is True
    assert store.events[0].success is False
    assert router.health_report()["first"].consecutive_failures == 0


def test_stop_category_while_opening_replacement_aborts_remaining_protocols() -> None:
    first = _config("first", ApiProtocol.OPENAI_CHAT, "chat-model")
    second = _config("second", ApiProtocol.OPENAI_RESPONSES, "responses-model")
    third = _config("third", ApiProtocol.OPENAI_CHAT, "backup-chat-model")
    first_failure = _timeout(first)
    replacement_failure = InvalidOperationArgumentsError(
        "bad replacement arguments",
        provider_id=second.name,
        provider_name=second.name,
        model=second.model,
    )
    script = _Script(
        {
            "first": [_FakeStream(["partial"], error=first_failure)],
            "second": [replacement_failure],
            "third": [_FakeStream(["must not run"], completed_at=0)],
        }
    )
    router, store = _router([first, second, third], script)
    routed = router.invoke(_calls(stream=True))

    assert next(routed) == "partial"
    with pytest.raises(RouterExhaustedError) as exc_info:
        next(routed)

    assert [call[0] for call in script.calls] == ["first", "second"]
    assert [attempt.error for attempt in exc_info.value.attempts] == [
        first_failure,
        replacement_failure,
    ]
    assert [event.stream_opened for event in store.events] == [True, False]
    assert router.health_report()["first"].consecutive_failures == 1
    assert router.health_report()["second"].consecutive_failures == 0


def test_stream_failure_policy_raise_prevents_cross_protocol_regeneration() -> None:
    first = _config("first", ApiProtocol.OPENAI_RESPONSES, "responses-model")
    second = _config("second", ApiProtocol.OPENAI_CHAT, "chat-model")
    failure = _timeout(first)
    script = _Script(
        {
            "first": [_FakeStream(["partial"], error=failure)],
            "second": [_FakeStream(["must not run"], completed_at=0)],
        }
    )
    router, _ = _router(
        [first, second],
        script,
        stream_failure_policy=StreamFailurePolicy.RAISE,
    )
    routed = router.invoke(_calls(stream=True))

    assert next(routed) == "partial"
    with pytest.raises(ProviderTimeoutError):
        next(routed)

    assert [call[0] for call in script.calls] == ["first"]


def test_retryable_stream_failure_restarts_across_protocols() -> None:
    first = _config("first", ApiProtocol.OPENAI_RESPONSES, "responses-model")
    second = _config("second", ApiProtocol.OPENAI_CHAT, "chat-model")
    first_failure = _timeout(first)
    script = _Script(
        {
            "first": [_FakeStream(["partial"], error=first_failure)],
            "second": [_FakeStream(["replacement"], completed_at=0)],
        }
    )
    router, store = _router([first, second], script)
    routed = router.invoke(_calls(stream=True))

    assert list(routed) == ["partial", "replacement"]
    assert routed.restarts == 1
    assert [call[1] for call in script.calls] == [
        "responses.create",
        "chat.completions.create",
    ]
    assert [event.success for event in store.events] == [False, True]
    assert all(event.stream_opened for event in store.events)


def _responses_stream_body(*events: dict[str, object]) -> bytes:
    return (
        b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)
        + b"data: [DONE]\n\n"
    )


def _response_body(status: str, *, reason: str | None = None) -> dict[str, object]:
    return {
        "id": "resp_router",
        "object": "response",
        "created_at": 0.0,
        "status": status,
        "model": "responses-model",
        "output": [],
        "parallel_tool_calls": True,
        "tools": [],
        "tool_choice": "auto",
        "incomplete_details": None if reason is None else {"reason": reason},
        "usage": {
            "input_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 4,
        },
    }


def test_real_incomplete_responses_stream_is_served_without_fallback_or_bench(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _config("responses", ApiProtocol.OPENAI_RESPONSES, "responses-model")
    body = _responses_stream_body(
        {
            "type": "response.output_text.delta",
            "sequence_number": 0,
            "item_id": "msg",
            "output_index": 0,
            "content_index": 0,
            "delta": "partial",
            "logprobs": [],
        },
        {
            "type": "response.incomplete",
            "sequence_number": 1,
            "response": _response_body("incomplete", reason="content_filter"),
        },
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )
        )
    )
    store = _Store()
    router = ProviderRouter(
        [provider],
        metrics_scope="test",
        adapter_factory=lambda config: OpenAIResponsesAdapter(config, http_client=client),
        metrics_store=store,  # type: ignore[arg-type]
    )

    with caplog.at_level("WARNING"):
        stream = router.invoke([_calls(stream=True)[1]])
        events = list(stream)

    assert [event.type for event in events] == [
        "response.output_text.delta",
        "response.incomplete",
    ]
    assert stream.usage.total_tokens == 4
    assert len(store.events) == 1
    assert store.events[0].success is True
    assert store.events[0].stream_opened is True
    assert store.events[0].latency_ms is not None
    assert store.events[0].total_duration_ms is not None
    assert router.health_report()["responses"].consecutive_failures == 0
    assert sum("content_filter" in record.getMessage() for record in caplog.records) == 1


@pytest.mark.parametrize(
    "events",
    [
        [],
        [
            {
                "type": "response.output_text.delta",
                "sequence_number": 0,
                "item_id": "msg",
                "output_index": 0,
                "content_index": 0,
                "delta": "partial",
                "logprobs": [],
            }
        ],
    ],
    ids=["empty", "recognized-without-terminal"],
)
def test_real_responses_stream_without_usable_terminal_result_is_interrupted(
    events: list[dict[str, object]],
) -> None:
    provider = _config("responses", ApiProtocol.OPENAI_RESPONSES, "responses-model")
    body = _responses_stream_body(*events)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )
        )
    )
    store = _Store()
    router = ProviderRouter(
        [provider],
        metrics_scope="test",
        adapter_factory=lambda config: OpenAIResponsesAdapter(config, http_client=client),
        metrics_store=store,  # type: ignore[arg-type]
        stream_failure_policy=StreamFailurePolicy.RAISE,
    )
    stream = router.invoke([_calls(stream=True)[1]])

    yielded = [next(stream)] if events else []
    assert len(yielded) == len(events)
    with pytest.raises(ProviderStreamInterruptedError):
        next(stream)

    assert len(store.events) == 1
    assert store.events[0].success is False
    assert store.events[0].stream_opened is True
