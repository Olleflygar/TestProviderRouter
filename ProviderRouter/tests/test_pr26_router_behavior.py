from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from metrics_store_helpers import zero_score_aggregates

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ConfigError,
    HealthConfig,
    MetricsEvent,
    NormalizedStream,
    ProviderConfig,
    ProviderHTTPError,
    ProviderRouter,
    ProviderStreamInterruptedError,
    ProviderTimeoutError,
    RouterExhaustedError,
    RoutingContext,
    ScoreAggregate,
    ScoreAggregateQuery,
    StickyRoutingPolicy,
    StreamFailurePolicy,
)


def _provider(
    provider_id: str,
    *,
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
    enabled: bool = True,
    api_key: str | None = "secret",
    api_key_env: str | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=provider_id,
        protocol=protocol,
        model="model-a",
        base_url=f"https://{provider_id}.example.com/v1",
        api_key=api_key,
        api_key_env=api_key_env,
        enabled=enabled,
    )


def _regular_calls() -> list[CallVariant]:
    return [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.REGULAR,
            arguments={"messages": [{"role": "user", "content": "hi"}]},
        )
    ]


def _streaming_calls() -> list[CallVariant]:
    return [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.STREAMING,
            arguments={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    ]


def _timeout(provider_id: str) -> ProviderTimeoutError:
    return ProviderTimeoutError(
        f"{provider_id} timed out",
        provider_id=provider_id,
        provider_name=provider_id,
        model="model-a",
    )


def _http(provider_id: str, status_code: int) -> ProviderHTTPError:
    return ProviderHTTPError(
        provider_id=provider_id,
        provider_name=provider_id,
        model="model-a",
        status_code=status_code,
        message=f"status {status_code}",
    )


class _IdentityPolicy:
    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(eligible)


class _Script:
    def __init__(self, behaviors: dict[str, list[Any]]) -> None:
        self.behaviors = {provider_id: list(items) for provider_id, items in behaviors.items()}
        self.invoked: list[str] = []

    def next(self, provider_id: str) -> Any:
        self.invoked.append(provider_id)
        queue = self.behaviors.get(provider_id)
        if not queue:
            raise AssertionError(f"No behavior scripted for {provider_id!r}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Adapter:
    def __init__(self, config: ProviderConfig, script: _Script) -> None:
        self._provider_id = config.provider_id
        self._script = script

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        return self._script.next(self._provider_id)


def _router(
    providers: list[ProviderConfig],
    script: _Script,
    *,
    sticky_provider_ids: list[str],
    clock: Callable[[], float] = lambda: 0.0,
    health: HealthConfig | None = None,
    metrics_store: Any = None,
    stream_failure_policy: StreamFailurePolicy = StreamFailurePolicy.RESTART,
) -> ProviderRouter:
    def factory(config: ProviderConfig) -> _Adapter:
        return _Adapter(config, script)

    return ProviderRouter(
        providers=providers,
        metrics_scope="test",
        adapter_factory=factory,
        policy=StickyRoutingPolicy(
            sticky_provider_ids=sticky_provider_ids,
            fallback_policy=_IdentityPolicy(),
        ),
        clock=clock,
        health=health,
        metrics_store=metrics_store,
        stream_failure_policy=stream_failure_policy,
    )


@pytest.mark.parametrize(
    "preferred",
    [
        _provider("disabled", enabled=False),
        _provider("missing-key", api_key=None, api_key_env="PR26_MISSING_KEY"),
        _provider("unsupported", protocol=ApiProtocol.ANTHROPIC_MESSAGES),
        _provider("wrong-variant", protocol=ApiProtocol.OPENAI_RESPONSES),
    ],
    ids=["disabled", "missing-key", "unsupported-adapter", "missing-call-variant"],
)
def test_hard_filters_override_sticky_preference(
    preferred: ProviderConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PR26_MISSING_KEY", raising=False)
    tail = _provider("tail")
    script = _Script({"tail": ["tail-response"]})
    router = _router(
        [preferred, tail],
        script,
        sticky_provider_ids=[preferred.provider_id],
    )

    assert router.invoke(_regular_calls()) == "tail-response"
    assert script.invoked == ["tail"]


def test_retryable_failure_uses_remaining_composed_order_and_preserves_preference() -> None:
    a, b, tail = _provider("a"), _provider("b"), _provider("tail")
    fallback_response = object()
    first_response = object()
    second_response = object()
    script = _Script(
        {
            "a": [_timeout("a"), first_response, second_response],
            "b": [_timeout("b")],
            "tail": [fallback_response],
        }
    )
    router = _router([tail, b, a], script, sticky_provider_ids=["a", "b"])

    assert router.invoke(_regular_calls()) is fallback_response
    assert script.invoked == ["a", "b", "tail"]

    # A successful tail did not rewrite fixed preference: a leads again.
    assert router.invoke(_regular_calls()) is first_response
    assert router.invoke(_regular_calls()) is second_response
    assert script.invoked[-2:] == ["a", "a"]


def test_bad_request_stays_fail_fast_and_does_not_change_health() -> None:
    preferred, other = _provider("preferred"), _provider("other")
    script = _Script({"preferred": [_http("preferred", 400), "later-success"]})
    router = _router([other, preferred], script, sticky_provider_ids=["preferred"])

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke(_regular_calls())

    assert [attempt.provider_id for attempt in exc_info.value.attempts] == ["preferred"]
    assert router.health_report()["preferred"].consecutive_failures == 0
    assert router.invoke(_regular_calls()) == "later-success"
    assert script.invoked == ["preferred", "preferred"]


def test_rate_limit_cooldown_skips_then_restores_fixed_preference() -> None:
    now = [10.0]
    preferred, tail = _provider("preferred"), _provider("tail")
    restored = object()
    script = _Script(
        {
            "preferred": [_http("preferred", 429), restored],
            "tail": ["fallback", "during-cooldown"],
        }
    )
    router = _router(
        [tail, preferred],
        script,
        sticky_provider_ids=["preferred"],
        clock=lambda: now[0],
        health=HealthConfig(rate_limit_cooldown_seconds=5.0),
    )

    assert router.invoke(_regular_calls()) == "fallback"
    assert router.invoke(_regular_calls()) == "during-cooldown"
    now[0] = 16.0
    assert router.invoke(_regular_calls()) is restored
    assert script.invoked == ["preferred", "tail", "tail", "preferred"]


def test_counted_failure_below_threshold_keeps_preferred_provider_first() -> None:
    preferred, tail = _provider("preferred"), _provider("tail")
    preferred_response = object()
    script = _Script(
        {
            "preferred": [_timeout("preferred"), preferred_response],
            "tail": ["fallback"],
        }
    )
    router = _router(
        [tail, preferred],
        script,
        sticky_provider_ids=["preferred"],
        health=HealthConfig(failure_threshold=2),
    )

    assert router.invoke(_regular_calls()) == "fallback"
    assert router.invoke(_regular_calls()) is preferred_response
    assert script.invoked == ["preferred", "tail", "preferred"]


def test_removing_disabling_and_restoring_provider_updates_only_eligibility() -> None:
    preferred, tail = _provider("preferred"), _provider("tail")
    script = _Script(
        {
            "preferred": ["first", "restored"],
            "tail": ["removed", "disabled"],
        }
    )
    router = _router([tail, preferred], script, sticky_provider_ids=["preferred"])

    assert router.invoke(_regular_calls()) == "first"
    router.providers = [tail]
    assert router.invoke(_regular_calls()) == "removed"
    disabled = preferred.model_copy(update={"enabled": False})
    router.providers = [disabled, tail]
    assert router.invoke(_regular_calls()) == "disabled"
    router.providers = [tail, preferred]
    assert router.invoke(_regular_calls()) == "restored"


def test_non_streaming_success_preserves_exact_response_identity() -> None:
    response = object()
    preferred = _provider("preferred")
    router = _router(
        [preferred],
        _Script({"preferred": [response]}),
        sticky_provider_ids=["preferred"],
    )

    assert router.invoke(_regular_calls()) is response


class _FakeStream(NormalizedStream):
    def __init__(
        self,
        chunks: list[object],
        *,
        completed: bool,
        error: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._completed = completed
        self._error = error
        self.closed = False

    def __next__(self) -> object:
        if self._chunks:
            return self._chunks.pop(0)
        if self._error is not None:
            raise self._error
        raise StopIteration

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def usage(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _MemoryStore:
    def __init__(self) -> None:
        self.events: list[MetricsEvent] = []

    def record_attempt(self, event: MetricsEvent) -> None:
        self.events.append(event)

    def query_recent(self, **kwargs: Any) -> list[MetricsEvent]:
        return list(self.events)

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        return zero_score_aggregates(query)


def test_interrupted_preferred_stream_restarts_without_rewriting_preference() -> None:
    first_chunk, fallback_chunk, later_chunk = object(), object(), object()
    preferred, tail = _provider("preferred"), _provider("tail")
    store = _MemoryStore()
    script = _Script(
        {
            "preferred": [
                _FakeStream(
                    [first_chunk],
                    completed=False,
                    error=_timeout("preferred"),
                ),
                _FakeStream([later_chunk], completed=True),
            ],
            "tail": [_FakeStream([fallback_chunk], completed=True)],
        }
    )
    router = _router(
        [tail, preferred],
        script,
        sticky_provider_ids=["preferred"],
        metrics_store=store,
    )

    assert list(router.invoke(_streaming_calls())) == [first_chunk, fallback_chunk]
    assert [event.provider_id for event in store.events] == ["preferred", "tail"]
    assert [event.success for event in store.events] == [False, True]
    assert list(router.invoke(_streaming_calls())) == [later_chunk]
    assert script.invoked == ["preferred", "tail", "preferred"]


def test_zero_chunk_stream_restarts_and_raise_policy_stops_restart() -> None:
    preferred, tail = _provider("preferred"), _provider("tail")
    tail_chunk = object()
    restart_router = _router(
        [preferred, tail],
        _Script(
            {
                "preferred": [_FakeStream([], completed=True)],
                "tail": [_FakeStream([tail_chunk], completed=True)],
            }
        ),
        sticky_provider_ids=["preferred"],
    )
    assert list(restart_router.invoke(_streaming_calls())) == [tail_chunk]

    script = _Script(
        {
            "preferred": [_FakeStream([], completed=True)],
            "tail": [_FakeStream([object()], completed=True)],
        }
    )
    raise_router = _router(
        [preferred, tail],
        script,
        sticky_provider_ids=["preferred"],
        stream_failure_policy=StreamFailurePolicy.RAISE,
    )
    with pytest.raises(ProviderStreamInterruptedError, match="without yielding any chunks"):
        list(raise_router.invoke(_streaming_calls()))
    assert script.invoked == ["preferred"]


def test_streaming_stop_failure_does_not_reach_the_tail() -> None:
    preferred, tail = _provider("preferred"), _provider("tail")
    script = _Script(
        {
            "preferred": [_FakeStream([], completed=False, error=_http("preferred", 400))],
            "tail": [_FakeStream([object()], completed=True)],
        }
    )
    router = _router([preferred, tail], script, sticky_provider_ids=["preferred"])

    with pytest.raises(ProviderHTTPError, match="status 400"):
        list(router.invoke(_streaming_calls()))
    assert script.invoked == ["preferred"]


def test_early_close_records_no_outcome_and_never_restarts() -> None:
    preferred, tail = _provider("preferred"), _provider("tail")
    chunk = object()
    store = _MemoryStore()
    script = _Script(
        {
            "preferred": [_FakeStream([chunk], completed=False)],
            "tail": [_FakeStream([object()], completed=True)],
        }
    )
    router = _router(
        [preferred, tail],
        script,
        sticky_provider_ids=["preferred"],
        metrics_store=store,
    )

    stream = router.invoke(_streaming_calls())
    assert next(stream) is chunk
    stream.close()

    assert store.events == []
    assert script.invoked == ["preferred"]


class _InvalidFallbackPolicy:
    def __init__(self, introduced: ProviderConfig) -> None:
        self._introduced = introduced

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return [self._introduced]


def test_invalid_wrapped_provider_is_rejected_before_adapter_invocation() -> None:
    preferred = _provider("preferred")
    tail = _provider("tail")
    disabled = _provider("disabled", enabled=False)
    script = _Script({"preferred": ["must-not-run"], "tail": ["must-not-run"]})

    def factory(config: ProviderConfig) -> _Adapter:
        return _Adapter(config, script)

    router = ProviderRouter(
        providers=[preferred, tail, disabled],
        metrics_scope="test",
        metrics_store=None,
        adapter_factory=factory,
        policy=StickyRoutingPolicy(
            sticky_provider_ids=["preferred"],
            fallback_policy=_InvalidFallbackPolicy(disabled),
        ),
    )

    with pytest.raises(ConfigError, match="not in its eligible non-sticky remainder"):
        router.invoke(_regular_calls())
    assert script.invoked == []
