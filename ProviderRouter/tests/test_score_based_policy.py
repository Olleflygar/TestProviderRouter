from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from nygen_router import (
    ApiProtocol,
    CallVariant,
    MetricsEvent,
    ProviderConfig,
    ProviderRouter,
    ProviderTimeoutError,
    RoundRobinPolicy,
    RoutingContext,
    ScoreBasedPolicy,
    filter_eligible_providers,
)


class _FakeStore:
    """In-memory MetricsStore that honors query_recent's filtering contract."""

    def __init__(self, events: list[MetricsEvent] | None = None) -> None:
        self.events = [] if events is None else list(events)
        self.queries: list[datetime] = []

    def record_attempt(self, event: MetricsEvent) -> None:
        self.events.append(event)

    def query_recent(
        self,
        *,
        since: datetime,
        provider_name: str | None = None,
        model: str | None = None,
    ) -> list[MetricsEvent]:
        self.queries.append(since)
        return [
            event
            for event in self.events
            if event.timestamp >= since
            and (provider_name is None or event.provider_name == provider_name)
            and (model is None or event.model == model)
        ]


class _FailingStore:
    def record_attempt(self, event: MetricsEvent) -> None:
        return None

    def query_recent(
        self,
        *,
        since: datetime,
        provider_name: str | None = None,
        model: str | None = None,
    ) -> list[MetricsEvent]:
        raise RuntimeError("history database is unavailable")


class _ReverseTiePolicy:
    """Deterministic tie-break policy whose exact returned list is observable."""

    def __init__(self) -> None:
        self.last_result: list[ProviderConfig] | None = None
        self.contexts: list[RoutingContext] = []

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        self.contexts.append(context)
        self.last_result = list(reversed(eligible))
        return self.last_result


class _ScriptedAdapter:
    def __init__(
        self,
        config: ProviderConfig,
        failures: dict[str, Exception],
        invoked: list[str],
    ) -> None:
        self.config = config
        self._failures = failures
        self._invoked = invoked

    def invoke(self, operation: str, arguments: dict[str, object]) -> str:
        self._invoked.append(self.config.name)
        failure = self._failures.get(self.config.name)
        if failure is not None:
            raise failure
        return self.config.name


def _config(name: str, *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
        enabled=enabled,
    )


def _event(
    provider_name: str,
    *,
    success: bool,
    timestamp: datetime,
    latency_ms: float | None = None,
    stream: bool = False,
) -> MetricsEvent:
    return MetricsEvent(
        provider_name=provider_name,
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        success=success,
        latency_ms=latency_ms,
        error_type=None if success else "server_error",
        stream=stream,
        timestamp=timestamp,
    )


def _calls() -> list[CallVariant]:
    return [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={"messages": [{"role": "user", "content": "hi"}]},
        )
    ]


def _outcome_events(
    provider_name: str,
    *,
    successes: int,
    failures: int,
    timestamp: datetime,
    latency_ms: float,
    stream: bool = False,
) -> list[MetricsEvent]:
    return [
        *[
            _event(
                provider_name,
                success=True,
                timestamp=timestamp,
                latency_ms=latency_ms,
                stream=stream,
            )
            for _ in range(successes)
        ],
        *[
            _event(provider_name, success=False, timestamp=timestamp, stream=stream)
            for _ in range(failures)
        ],
    ]


def test_best_score_orders_first_and_router_falls_back_to_the_next_ranked() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    providers = [_config("best"), _config("middle"), _config("worst")]
    store = _FakeStore(
        [
            *_outcome_events(
                "best", successes=20, failures=0, timestamp=fixed_now, latency_ms=50.0
            ),
            *_outcome_events(
                "middle", successes=12, failures=8, timestamp=fixed_now, latency_ms=500.0
            ),
            *_outcome_events(
                "worst", successes=0, failures=20, timestamp=fixed_now, latency_ms=5000.0
            ),
        ]
    )
    policy = ScoreBasedPolicy(
        tie_break_policy=_ReverseTiePolicy(),
        now=lambda: fixed_now,
    )
    context = RoutingContext(metrics_store=store)

    assert [provider.name for provider in policy.order(providers, context)] == [
        "best",
        "middle",
        "worst",
    ]

    invoked: list[str] = []
    failure = ProviderTimeoutError("best timed out", provider_name="best", model="model-a")

    def factory(config: ProviderConfig) -> _ScriptedAdapter:
        return _ScriptedAdapter(config, {"best": failure}, invoked)

    router = ProviderRouter(
        providers=providers,
        adapter_factory=factory,
        policy=policy,
        metrics_store=store,
    )

    assert router.invoke(_calls()) == "middle"
    assert invoked == ["best", "middle"]


def test_equal_scores_keep_default_round_robin_tie_break_order() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    context = RoutingContext(metrics_store=_FakeStore())
    score_policy = ScoreBasedPolicy()
    round_robin = RoundRobinPolicy()

    for _ in range(3):
        expected = round_robin.order(providers, context)
        actual = score_policy.order(providers, context)
        assert actual == expected


def test_no_metrics_store_returns_the_exact_tie_break_result() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    tie_break = _ReverseTiePolicy()
    context = RoutingContext(metrics_store=None)
    policy = ScoreBasedPolicy(tie_break_policy=tie_break)

    result = policy.order(providers, context)

    assert result is tie_break.last_result
    assert [provider.name for provider in result] == ["provider_b", "provider_a"]


def test_empty_eligible_list_returns_without_querying_history() -> None:
    store = _FakeStore()
    policy = ScoreBasedPolicy()

    assert policy.order([], RoutingContext(metrics_store=store)) == []
    assert store.queries == []


def test_query_failure_degrades_to_tie_break_order_and_deduplicates_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    tie_break = _ReverseTiePolicy()
    policy = ScoreBasedPolicy(tie_break_policy=tie_break)
    context = RoutingContext(metrics_store=_FailingStore())

    with caplog.at_level(logging.DEBUG, logger="nygen_router.policies.score_based"):
        first = policy.order(providers, context)
        second = policy.order(providers, context)

    assert [provider.name for provider in first] == ["provider_b", "provider_a"]
    assert [provider.name for provider in second] == ["provider_b", "provider_a"]
    history_logs = [
        record
        for record in caplog.records
        if "Metrics history is unavailable" in record.getMessage()
    ]
    assert [record.levelno for record in history_logs] == [logging.WARNING, logging.DEBUG]


def test_score_policy_only_receives_and_returns_eligible_providers() -> None:
    providers = [_config("eligible"), _config("disabled", enabled=False)]
    eligible, excluded = filter_eligible_providers(
        providers,
        supported_protocols={ApiProtocol.OPENAI_CHAT},
        requested_protocols={ApiProtocol.OPENAI_CHAT},
    )
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    store = _FakeStore(
        _outcome_events(
            "disabled",
            successes=100,
            failures=0,
            timestamp=fixed_now,
            latency_ms=1.0,
        )
    )
    policy = ScoreBasedPolicy(now=lambda: fixed_now)

    ordered = policy.order(eligible, RoutingContext(metrics_store=store))

    assert [provider.name for provider in ordered] == ["eligible"]
    assert [result.provider_name for result in excluded] == ["disabled"]


def test_regular_and_streaming_policy_instances_prefer_opposite_histories() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    providers = [_config("regular_star"), _config("streaming_star")]
    store = _FakeStore(
        [
            *_outcome_events(
                "regular_star",
                successes=20,
                failures=0,
                timestamp=fixed_now,
                latency_ms=50.0,
            ),
            *_outcome_events(
                "regular_star",
                successes=0,
                failures=20,
                timestamp=fixed_now,
                latency_ms=5000.0,
                stream=True,
            ),
            *_outcome_events(
                "streaming_star",
                successes=0,
                failures=20,
                timestamp=fixed_now,
                latency_ms=5000.0,
            ),
            *_outcome_events(
                "streaming_star",
                successes=20,
                failures=0,
                timestamp=fixed_now,
                latency_ms=25.0,
                stream=True,
            ),
        ]
    )
    context = RoutingContext(metrics_store=store)
    regular_policy = ScoreBasedPolicy(use_streaming=False, now=lambda: fixed_now)
    streaming_policy = ScoreBasedPolicy(use_streaming=True, now=lambda: fixed_now)

    regular_order = regular_policy.order(providers, context)
    streaming_order = streaming_policy.order(providers, context)

    assert [provider.name for provider in regular_order] == [
        "regular_star",
        "streaming_star",
    ]
    assert [provider.name for provider in streaming_order] == [
        "streaming_star",
        "regular_star",
    ]


def test_event_older_than_lookback_has_zero_effect() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    providers = [_config("provider_a"), _config("provider_b")]
    old_successes = _outcome_events(
        "provider_a",
        successes=100,
        failures=0,
        timestamp=fixed_now - timedelta(hours=25),
        latency_ms=1.0,
    )
    store = _FakeStore(old_successes)
    tie_break = _ReverseTiePolicy()
    policy = ScoreBasedPolicy(
        lookback_hours=24.0,
        tie_break_policy=tie_break,
        now=lambda: fixed_now,
    )

    ordered = policy.order(providers, RoutingContext(metrics_store=store))

    assert store.queries == [fixed_now - timedelta(hours=24)]
    assert [provider.name for provider in ordered] == ["provider_b", "provider_a"]


@pytest.mark.parametrize("lookback_hours", [0.0, -1.0])
def test_non_positive_lookback_is_rejected(lookback_hours: float) -> None:
    with pytest.raises(ValueError, match="lookback_hours must be positive"):
        ScoreBasedPolicy(lookback_hours=lookback_hours)


def test_routing_context_is_frozen() -> None:
    context = RoutingContext(metrics_store=None)

    with pytest.raises(FrozenInstanceError):
        context.metrics_store = _FakeStore()  # type: ignore[misc]


def test_custom_two_argument_policy_is_honored_and_receives_the_routers_store() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    store = _FakeStore()
    policy = _ReverseTiePolicy()
    invoked: list[str] = []

    def factory(config: ProviderConfig) -> _ScriptedAdapter:
        return _ScriptedAdapter(config, {}, invoked)

    router = ProviderRouter(
        providers=providers,
        adapter_factory=factory,
        policy=policy,
        metrics_store=store,
    )

    assert router.invoke(_calls()) == "provider_b"
    assert router.invoke(_calls()) == "provider_b"
    assert invoked == ["provider_b", "provider_b"]
    assert len(policy.contexts) == 2
    assert policy.contexts[0] is not policy.contexts[1]
    assert policy.contexts[0].metrics_store is store
    assert policy.contexts[1].metrics_store is store
