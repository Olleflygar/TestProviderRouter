from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from metrics_store_helpers import aggregate_events_for_score_query

from nygen_router import (
    ApiProtocol,
    CallType,
    MetricsEvent,
    ProviderConfig,
    RoutingContext,
    ScoreAggregate,
    ScoreAggregateQuery,
    ScoreBasedPolicy,
    ScoreWeights,
    aggregate_stats,
    calculate_provider_score,
)


class _FakeStore:
    """In-memory store that records and honors aggregate requests."""

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


class _StaticPolicy:
    def __init__(self, *, reverse: bool = False) -> None:
        self._reverse = reverse

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(reversed(eligible)) if self._reverse else list(eligible)


def _config(name: str) -> ProviderConfig:
    return ProviderConfig(
        provider_id=name,
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
    )


def _event(
    provider_name: str,
    *,
    timestamp: datetime,
    success: bool,
    error_type: str | None = None,
    latency_ms: float | None = None,
) -> MetricsEvent:
    return MetricsEvent(
        provider_id=provider_name,
        metrics_scope="test",
        call_type=CallType.REGULAR,
        provider_name=provider_name,
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        success=success,
        latency_ms=latency_ms,
        error_type=error_type,
        timestamp=timestamp,
    )


def _names(providers: list[ProviderConfig]) -> list[str]:
    return [provider.name for provider in providers]


def test_explicit_none_is_identical_to_not_passing_half_life() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    providers = [_config("provider_a"), _config("provider_b")]
    events = [
        _event(
            "provider_a",
            timestamp=fixed_now - timedelta(hours=300),
            success=True,
            latency_ms=100.0,
        ),
        _event(
            "provider_b",
            timestamp=fixed_now - timedelta(hours=10),
            success=False,
            error_type="server_error",
        ),
    ]
    omitted_store = _FakeStore(events)
    explicit_store = _FakeStore(events)
    omitted = ScoreBasedPolicy(tie_break_policy=_StaticPolicy(), now=lambda: fixed_now)
    explicit_none = ScoreBasedPolicy(
        half_life_hours=None,
        tie_break_policy=_StaticPolicy(),
        now=lambda: fixed_now,
    )

    omitted_result = omitted.order(
        providers,
        RoutingContext(
            metrics_scope="test", call_type=CallType.REGULAR, metrics_store=omitted_store
        ),
    )
    explicit_result = explicit_none.order(
        providers,
        RoutingContext(
            metrics_scope="test", call_type=CallType.REGULAR, metrics_store=explicit_store
        ),
    )

    assert omitted_result == explicit_result
    assert [query.since for query in omitted_store.queries] == [
        query.since for query in explicit_store.queries
    ]
    assert [query.since for query in omitted_store.queries] == [fixed_now - timedelta(hours=336)]
    assert omitted_store.raw_query_calls == explicit_store.raw_query_calls == 0


def test_five_half_life_old_event_has_a_small_but_nonzero_effect() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    half_life_hours = 24.0
    old_event = _event(
        "old_history",
        timestamp=fixed_now - timedelta(hours=5 * half_life_hours),
        success=True,
        latency_ms=100.0,
    )
    providers = [_config("old_history"), _config("no_history")]
    store = _FakeStore([old_event])
    policy = ScoreBasedPolicy(
        weights=ScoreWeights(success_weight=1.0, speed_weight=0.0),
        half_life_hours=half_life_hours,
        tie_break_policy=_StaticPolicy(reverse=True),
        now=lambda: fixed_now,
    )

    ordered = policy.order(
        providers,
        RoutingContext(metrics_scope="test", call_type=CallType.REGULAR, metrics_store=store),
    )

    assert _names(ordered) == ["old_history", "no_history"]

    def decay_weight(event: MetricsEvent) -> float:
        age_hours = (fixed_now - event.timestamp).total_seconds() / 3600.0
        return 0.5 ** (age_hours / half_life_hours)

    decayed_stats = aggregate_stats(
        [old_event], [_config("old_history")], CallType.REGULAR, weight_fn=decay_weight
    )["old_history"]
    empty_stats = aggregate_stats([], [_config("no_history")], CallType.REGULAR)["no_history"]
    weights = ScoreWeights(success_weight=1.0, speed_weight=0.0)
    effect = (
        calculate_provider_score(decayed_stats, weights).total
        - calculate_provider_score(empty_stats, weights).total
    )
    assert 0 < effect < 0.01


def test_recent_failure_lowers_score_more_than_old_failure() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    half_life_hours = 24.0
    providers = [_config("recent_failure"), _config("old_failure")]
    store = _FakeStore(
        [
            _event(
                "recent_failure",
                timestamp=fixed_now,
                success=False,
                error_type="server_error",
            ),
            _event(
                "old_failure",
                timestamp=fixed_now - timedelta(hours=4 * half_life_hours),
                success=False,
                error_type="server_error",
            ),
        ]
    )
    policy = ScoreBasedPolicy(
        weights=ScoreWeights(success_weight=1.0, speed_weight=0.0),
        half_life_hours=half_life_hours,
        tie_break_policy=_StaticPolicy(),
        now=lambda: fixed_now,
    )

    ordered = policy.order(
        providers,
        RoutingContext(metrics_scope="test", call_type=CallType.REGULAR, metrics_store=store),
    )

    assert _names(ordered) == ["old_failure", "recent_failure"]


def test_recent_success_raises_score_more_than_old_success() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    half_life_hours = 24.0
    providers = [_config("old_success"), _config("recent_success")]
    store = _FakeStore(
        [
            _event(
                "old_success",
                timestamp=fixed_now - timedelta(hours=4 * half_life_hours),
                success=True,
                latency_ms=100.0,
            ),
            _event(
                "recent_success",
                timestamp=fixed_now,
                success=True,
                latency_ms=100.0,
            ),
        ]
    )
    policy = ScoreBasedPolicy(
        weights=ScoreWeights(success_weight=1.0, speed_weight=0.0),
        half_life_hours=half_life_hours,
        tie_break_policy=_StaticPolicy(),
        now=lambda: fixed_now,
    )

    ordered = policy.order(
        providers,
        RoutingContext(metrics_scope="test", call_type=CallType.REGULAR, metrics_store=store),
    )

    assert _names(ordered) == ["recent_success", "old_success"]


def test_decay_query_bound_is_exactly_six_half_lives_and_ignores_lookback() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    store = _FakeStore()
    policy = ScoreBasedPolicy(
        lookback_hours=1.0,
        half_life_hours=72.0,
        tie_break_policy=_StaticPolicy(),
        now=lambda: fixed_now,
    )

    policy.order(
        [_config("provider")],
        RoutingContext(metrics_scope="test", call_type=CallType.REGULAR, metrics_store=store),
    )

    assert [query.since for query in store.queries] == [fixed_now - timedelta(hours=6 * 72)]
    assert store.raw_query_calls == 0


def test_event_older_than_six_half_lives_is_not_considered() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    half_life_hours = 24.0
    providers = [_config("old_history"), _config("no_history")]
    store = _FakeStore(
        [
            _event(
                "old_history",
                timestamp=fixed_now - timedelta(hours=7 * half_life_hours),
                success=True,
                latency_ms=1.0,
            )
        ]
    )
    policy = ScoreBasedPolicy(
        half_life_hours=half_life_hours,
        tie_break_policy=_StaticPolicy(reverse=True),
        now=lambda: fixed_now,
    )

    ordered = policy.order(
        providers,
        RoutingContext(metrics_scope="test", call_type=CallType.REGULAR, metrics_store=store),
    )

    assert _names(ordered) == ["no_history", "old_history"]


@pytest.mark.parametrize("half_life_hours", [0.0, -1.0])
def test_non_positive_half_life_is_rejected(half_life_hours: float) -> None:
    with pytest.raises(ValueError, match="half_life_hours must be positive"):
        ScoreBasedPolicy(half_life_hours=half_life_hours)


def test_diagnostic_counts_are_identical_with_and_without_decay() -> None:
    fixed_now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    events = [
        _event(
            "provider",
            timestamp=fixed_now,
            success=False,
            error_type="rate_limit",
        ),
        _event(
            "provider",
            timestamp=fixed_now - timedelta(hours=12),
            success=False,
            error_type="timeout",
        ),
        _event(
            "provider",
            timestamp=fixed_now - timedelta(hours=24),
            success=False,
            error_type="server_error",
        ),
    ]

    flat = aggregate_stats(events, [_config("provider")], CallType.REGULAR)["provider"]
    decayed = aggregate_stats(
        events,
        [_config("provider")],
        CallType.REGULAR,
        weight_fn=lambda event: (
            0.5 ** (((fixed_now - event.timestamp).total_seconds() / 3600.0) / 24.0)
        ),
    )["provider"]

    assert decayed.recent_error_count == flat.recent_error_count == 3
    assert decayed.rate_limit_count == flat.rate_limit_count == 1
    assert decayed.timeout_count == flat.timeout_count == 1
