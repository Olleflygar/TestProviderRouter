from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metrics_store_helpers import aggregate_events_for_score_query
from pydantic import ValidationError

from llm_provider_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ConfigError,
    HistoryScope,
    MetricsEvent,
    MixedCallTypeError,
    ProviderConfig,
    ProviderRouter,
    ProviderTimeoutError,
    RouterExhaustedError,
    RoutingContext,
    ScoreAggregate,
    ScoreAggregateQuery,
    ScoreBasedPolicy,
    ScoreWeights,
    aggregate_stats,
)


class _Store:
    def __init__(self, events: list[MetricsEvent] | None = None) -> None:
        self.events = [] if events is None else list(events)
        self.queries: list[ScoreAggregateQuery] = []
        self.raw_query_calls = 0

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
        self.raw_query_calls += 1
        raise AssertionError("ScoreBasedPolicy must not query raw history")

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        self.queries.append(query)
        return aggregate_events_for_score_query(self.events, query)


class _Adapter:
    def __init__(self, outcome: object, calls: list[dict[str, object]]) -> None:
        self._outcome = outcome
        self._calls = calls

    def invoke(self, operation: str, arguments: dict[str, object]) -> object:
        self._calls.append(arguments)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _provider(
    provider_id: str,
    *,
    name: str = "Shared display",
    model: str = "model-a",
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=name,
        protocol=protocol,
        model=model,
        base_url="https://provider.example.com/v1",
        api_key="test-key-value",
    )


def _call(
    call_type: CallType = CallType.REGULAR,
    *,
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
    arguments: dict[str, object] | None = None,
) -> CallVariant:
    return CallVariant(
        protocol=protocol,
        operation=(
            "responses.create"
            if protocol is ApiProtocol.OPENAI_RESPONSES
            else "chat.completions.create"
        ),
        call_type=call_type,
        arguments={"messages": []} if arguments is None else arguments,
    )


def _event(
    provider: ProviderConfig,
    *,
    scope: str,
    success: bool,
    call_type: CallType = CallType.REGULAR,
    provider_name: str | None = None,
) -> MetricsEvent:
    return MetricsEvent(
        metrics_scope=scope,
        provider_id=provider.provider_id,
        provider_name=provider.name if provider_name is None else provider_name,
        model=provider.model,
        protocol=provider.protocol,
        call_type=call_type,
        success=success,
        stream_opened=None if call_type is CallType.REGULAR else True,
        latency_ms=10.0 if success else None,
        error_type=None if success else "server_error",
        timestamp=datetime.now(UTC),
    )


def test_provider_id_and_metrics_scope_are_required_trimmed_and_nonblank() -> None:
    with pytest.raises(ValidationError, match="provider_id"):
        ProviderConfig.model_validate(
            {
                "name": "display",
                "protocol": ApiProtocol.OPENAI_CHAT,
                "model": "model-a",
                "base_url": "https://provider.example.com/v1",
                "api_key": "secret",
            }
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        _provider("   ")

    provider = _provider("  stable-id  ")
    router = ProviderRouter([provider], metrics_scope="  app:production  ", metrics_store=None)

    assert provider.provider_id == "stable-id"
    assert router.metrics_scope == "app:production"
    with pytest.raises(TypeError):
        ProviderRouter([provider], metrics_store=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigError, match="must not be empty"):
        ProviderRouter([provider], metrics_scope="   ", metrics_store=None)


def test_call_type_is_required_and_accepts_the_public_string_values() -> None:
    with pytest.raises(ValidationError, match="call_type"):
        CallVariant.model_validate(
            {
                "protocol": ApiProtocol.OPENAI_CHAT,
                "operation": "chat.completions.create",
                "arguments": {"messages": []},
            }
        )

    call = CallVariant.model_validate(
        {
            "protocol": ApiProtocol.OPENAI_CHAT,
            "operation": "chat.completions.create",
            "call_type": "streaming",
            "arguments": {"messages": [], "stream": True},
        }
    )

    assert call.call_type is CallType.STREAMING


def test_duplicate_names_are_allowed_but_every_duplicate_id_is_reported() -> None:
    ProviderRouter(
        [_provider("first"), _provider("second")],
        metrics_scope="test",
        metrics_store=None,
    )

    with pytest.raises(ConfigError) as exc_info:
        ProviderRouter(
            [_provider("duplicate"), _provider("other"), _provider("duplicate")],
            metrics_scope="test",
            metrics_store=None,
        )

    assert "duplicate" in str(exc_info.value)


def test_mixed_call_types_fail_before_adapter_health_or_metrics_are_touched() -> None:
    store = _Store()
    adapter_calls: list[dict[str, object]] = []
    router = ProviderRouter(
        [_provider("provider-a")],
        metrics_scope="test",
        metrics_store=store,
        adapter_factory=lambda _: _Adapter("response", adapter_calls),
    )

    with pytest.raises(MixedCallTypeError) as exc_info:
        router.invoke(
            [
                _call(CallType.REGULAR),
                _call(CallType.STREAMING, protocol=ApiProtocol.OPENAI_RESPONSES),
            ]
        )

    assert "openai_chat=regular" in str(exc_info.value)
    assert "openai_responses=streaming" in str(exc_info.value)
    assert adapter_calls == []
    assert store.events == []
    assert router.health_report()["provider-a"].consecutive_failures == 0


def test_native_stream_argument_is_never_inspected_or_reconciled() -> None:
    seen: list[dict[str, object]] = []
    original = {"messages": [], "stream": True, "custom": object()}
    router = ProviderRouter(
        [_provider("provider-a")],
        metrics_scope="test",
        metrics_store=None,
        adapter_factory=lambda _: _Adapter("raw response", seen),
    )

    assert router.invoke([_call(CallType.REGULAR, arguments=original)]) == "raw response"
    assert seen == [{**original, "model": "model-a"}]
    assert original == {"messages": [], "stream": True, "custom": original["custom"]}


def test_duplicate_named_providers_keep_attempts_health_and_metrics_separate_by_id() -> None:
    store = _Store()
    failures = {
        "first": ProviderTimeoutError(
            "first failed",
            provider_id="first",
            provider_name="Shared display",
            model="model-a",
        ),
        "second": ProviderTimeoutError(
            "second failed",
            provider_id="second",
            provider_name="Shared display",
            model="model-a",
        ),
    }
    router = ProviderRouter(
        [_provider("first"), _provider("second")],
        metrics_scope="test",
        metrics_store=store,
        health={"failure_threshold": 1},
        adapter_factory=lambda provider: _Adapter(failures[provider.provider_id], []),
    )

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke([_call()])

    assert [attempt.provider_id for attempt in exc_info.value.attempts] == ["first", "second"]
    assert [event.provider_id for event in store.events] == ["first", "second"]
    assert set(router.health_report()) == {"first", "second"}
    assert all(
        report.provider_name == "Shared display" for report in router.health_report().values()
    )
    assert 'Shared display (id="first")' in str(exc_info.value)
    assert 'Shared display (id="second")' in str(exc_info.value)
    router.reset_health("first")
    assert router.health_report()["first"].cooldown_remaining_seconds is None
    assert router.health_report()["second"].cooldown_remaining_seconds is not None


def test_aggregation_matches_id_model_protocol_and_call_type_but_not_old_display_name() -> None:
    provider = _provider("stable", name="Current name")
    events = [
        _event(provider, scope="scope", success=True, provider_name="Former name"),
        _event(_provider("other"), scope="scope", success=False),
        _event(_provider("stable", model="model-b"), scope="scope", success=False),
        _event(
            _provider("stable", protocol=ApiProtocol.OPENAI_RESPONSES),
            scope="scope",
            success=False,
        ),
        _event(provider, scope="scope", success=False, call_type=CallType.STREAMING),
    ]

    stats = aggregate_stats(events, [provider], CallType.REGULAR)["stable"]

    assert stats.provider_name == "Current name"
    assert stats.regular_attempt_count == 1
    assert stats.regular_success_rate == 1
    assert stats.recent_error_count == 0


def test_history_scope_current_and_all_make_one_query_and_can_rank_differently() -> None:
    fixed_now = datetime.now(UTC)
    first = _provider("first", name="First")
    second = _provider("second", name="Second")
    store = _Store(
        [
            *[_event(first, scope="current", success=False) for _ in range(5)],
            *[_event(second, scope="current", success=True) for _ in range(5)],
            *[_event(first, scope="other", success=True) for _ in range(100)],
        ]
    )
    context = RoutingContext(
        metrics_store=store,
        metrics_scope="current",
        call_type=CallType.REGULAR,
    )
    weights = ScoreWeights(success_weight=1, speed_weight=0)

    current = ScoreBasedPolicy(
        weights=weights, history_scope=HistoryScope.CURRENT, now=lambda: fixed_now
    ).order([first, second], context)
    all_scopes = ScoreBasedPolicy(
        weights=weights, history_scope=HistoryScope.ALL, now=lambda: fixed_now
    ).order([first, second], context)

    assert [provider.provider_id for provider in current] == ["second", "first"]
    assert [provider.provider_id for provider in all_scopes] == ["first", "second"]
    assert [query.metrics_scope for query in store.queries] == ["current", None]
    assert store.raw_query_calls == 0


def test_streaming_failure_before_open_has_streaming_classification_and_duration() -> None:
    store = _Store()
    error = ProviderTimeoutError(
        "timed out",
        provider_id="provider-a",
        provider_name="Provider A",
        model="model-a",
    )
    router = ProviderRouter(
        [_provider("provider-a", name="Provider A")],
        metrics_scope="test",
        metrics_store=store,
        adapter_factory=lambda _: _Adapter(error, []),
    )

    with pytest.raises(RouterExhaustedError):
        router.invoke([_call(CallType.STREAMING, arguments={"messages": [], "stream": True})])

    (event,) = store.events
    assert event.call_type is CallType.STREAMING
    assert event.stream_opened is False
    assert event.success is False
    assert event.latency_ms is None
    assert event.total_duration_ms is not None
    assert event.total_duration_ms >= 0
