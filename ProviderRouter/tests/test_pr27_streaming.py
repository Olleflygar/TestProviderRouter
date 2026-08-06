from __future__ import annotations

from typing import Any

import pytest
from metrics_store_helpers import zero_score_aggregates

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    MetricsEvent,
    NormalizedStream,
    ProviderConfig,
    ProviderHTTPError,
    ProviderRouter,
    ProviderStreamInterruptedError,
    ProviderTimeoutError,
    RouterExhaustedError,
    RoutingContext,
    SameProviderRetryPolicy,
    ScoreAggregate,
    ScoreAggregateQuery,
    StreamFailurePolicy,
)


def _provider(provider_id: str) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=f"display-{provider_id}",
        protocol=ApiProtocol.OPENAI_CHAT,
        model=f"model-{provider_id}",
        base_url=f"https://{provider_id}.example.com/v1",
        api_key="secret",
    )


def _calls() -> list[CallVariant]:
    return [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.STREAMING,
            arguments={"messages": [], "stream": True},
        )
    ]


def _timeout(provider_id: str) -> ProviderTimeoutError:
    return ProviderTimeoutError(
        f"{provider_id} timed out",
        provider_id=provider_id,
        provider_name=f"display-{provider_id}",
        model=f"model-{provider_id}",
    )


class _FakeStream(NormalizedStream):
    def __init__(
        self,
        chunks: list[object],
        *,
        completed: bool,
        error: Exception | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self._completed = completed
        self.error = error
        self.closed = False

    def __next__(self) -> object:
        if self.chunks:
            return self.chunks.pop(0)
        if self.error is not None:
            raise self.error
        raise StopIteration

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def usage(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Script:
    def __init__(self, behaviors: dict[str, list[Any]]) -> None:
        self.behaviors = {provider_id: list(items) for provider_id, items in behaviors.items()}
        self.invoked: list[str] = []
        self.factories: list[str] = []
        self.argument_objects: list[dict[str, object]] = []

    def next(self, provider_id: str, arguments: dict[str, object]) -> Any:
        self.invoked.append(provider_id)
        self.argument_objects.append(arguments)
        arguments["mutated"] = True
        queue = self.behaviors.get(provider_id)
        if not queue:
            raise AssertionError(f"No behavior for {provider_id!r}")
        behavior = queue.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _Adapter:
    def __init__(self, provider_id: str, script: _Script) -> None:
        self.provider_id = provider_id
        self.script = script

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        return self.script.next(self.provider_id, arguments)


class _StaticPolicy:
    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(eligible)


class _MemoryStore:
    def __init__(self) -> None:
        self.events: list[MetricsEvent] = []

    def record_attempt(self, event: MetricsEvent) -> None:
        self.events.append(event)

    def query_recent(self, **kwargs: Any) -> list[MetricsEvent]:
        return list(self.events)

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        return zero_score_aggregates(query)


def _router(
    providers: list[ProviderConfig],
    script: _Script,
    *,
    store: _MemoryStore | None = None,
    stream_failure_policy: StreamFailurePolicy = StreamFailurePolicy.RESTART,
) -> ProviderRouter:
    def factory(provider: ProviderConfig) -> _Adapter:
        script.factories.append(provider.provider_id)
        return _Adapter(provider.provider_id, script)

    return ProviderRouter(
        providers=providers,
        metrics_scope="test",
        metrics_store=store,
        adapter_factory=factory,
        policy=_StaticPolicy(),
        retry_policy=SameProviderRetryPolicy(max_attempts=3, provider_scope="all"),
        stream_failure_policy=stream_failure_policy,
    )


def test_declared_stream_preopen_failure_retries_then_returns_raw_chunks() -> None:
    chunk = object()
    stream = _FakeStream([chunk], completed=True)
    store = _MemoryStore()
    script = _Script({"a": [_timeout("a"), stream]})
    calls = _calls()
    router = _router([_provider("a")], script, store=store)

    routed = router.invoke(calls)
    assert list(routed) == [chunk]

    assert script.invoked == ["a", "a"]
    assert script.factories == ["a"]
    assert len({id(arguments) for arguments in script.argument_objects}) == 2
    assert calls[0].arguments == {"messages": [], "stream": True}
    assert [event.success for event in store.events] == [False, True]
    assert [event.stream_opened for event in store.events] == [False, True]
    assert store.events[0].latency_ms is None
    assert store.events[0].total_duration_ms is not None


def test_partial_opened_stream_never_same_provider_retries_and_restart_uses_tail() -> None:
    first_chunk, tail_chunk = object(), object()
    first = _FakeStream([first_chunk], completed=False, error=_timeout("a"))
    tail = _FakeStream([tail_chunk], completed=True)
    script = _Script({"a": [first], "b": [tail]})
    router = _router([_provider("a"), _provider("b")], script)

    assert list(router.invoke(_calls())) == [first_chunk, tail_chunk]
    assert script.invoked == ["a", "b"]


def test_after_stream_open_even_tail_preopen_failure_gets_no_pr27_retry() -> None:
    first = _FakeStream([object()], completed=False, error=_timeout("a"))
    final_chunk = object()
    final = _FakeStream([final_chunk], completed=True)
    script = _Script(
        {
            "a": [first],
            "b": [_timeout("b")],
            "c": [final],
        }
    )

    chunks = list(
        _router([_provider("a"), _provider("b"), _provider("c")], script).invoke(_calls())
    )

    assert chunks[-1] is final_chunk
    assert script.invoked == ["a", "b", "c"]


def test_zero_chunk_opened_stream_restarts_next_provider_without_same_provider_retry() -> None:
    tail_chunk = object()
    script = _Script(
        {
            "a": [_FakeStream([], completed=True)],
            "b": [_FakeStream([tail_chunk], completed=True)],
        }
    )

    assert list(_router([_provider("a"), _provider("b")], script).invoke(_calls())) == [tail_chunk]
    assert script.invoked == ["a", "b"]


def test_raise_policy_records_and_raises_real_opened_stream_error_without_retry() -> None:
    error = _timeout("a")
    script = _Script(
        {
            "a": [_FakeStream([], completed=False, error=error)],
            "b": [_FakeStream([object()], completed=True)],
        }
    )

    with pytest.raises(ProviderTimeoutError) as exc_info:
        list(
            _router(
                [_provider("a"), _provider("b")],
                script,
                stream_failure_policy=StreamFailurePolicy.RAISE,
            ).invoke(_calls())
        )

    assert exc_info.value is error
    assert script.invoked == ["a"]


def test_opened_stream_stop_failure_never_retries_or_restarts() -> None:
    error = ProviderHTTPError(
        provider_id="a",
        provider_name="display-a",
        model="model-a",
        status_code=400,
        message="invalid input",
    )
    script = _Script(
        {
            "a": [_FakeStream([], completed=False, error=error)],
            "b": [_FakeStream([object()], completed=True)],
        }
    )

    with pytest.raises(ProviderHTTPError) as exc_info:
        list(_router([_provider("a"), _provider("b")], script).invoke(_calls()))

    assert exc_info.value is error
    assert script.invoked == ["a"]


def test_close_and_early_break_never_create_retry_or_outcome() -> None:
    chunk = object()
    stream = _FakeStream([chunk], completed=False)
    store = _MemoryStore()
    script = _Script({"a": [stream], "b": [_FakeStream([object()], completed=True)]})
    routed = _router([_provider("a"), _provider("b")], script, store=store).invoke(_calls())

    assert next(routed) is chunk
    routed.close()

    assert stream.closed is True
    assert script.invoked == ["a"]
    assert store.events == []


def test_zero_chunk_raise_keeps_real_interruption_type_and_never_retries() -> None:
    script = _Script({"a": [_FakeStream([], completed=True)]})

    with pytest.raises(ProviderStreamInterruptedError):
        list(
            _router(
                [_provider("a")],
                script,
                stream_failure_policy=StreamFailurePolicy.RAISE,
            ).invoke(_calls())
        )

    assert script.invoked == ["a"]


def test_preopen_retry_exhaustion_keeps_repeated_errors_in_router_exhaustion() -> None:
    errors = [_timeout("a"), _timeout("a"), _timeout("a")]
    script = _Script({"a": errors.copy()})

    with pytest.raises(RouterExhaustedError) as exc_info:
        _router([_provider("a")], script).invoke(_calls())

    assert [attempt.error for attempt in exc_info.value.attempts] == errors
