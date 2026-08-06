from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    ConfigError,
    DuckDBMetricsStore,
    ExponentialScoreWeighting,
    FlatScoreWeighting,
    HistoryScope,
    ProviderConfig,
    ProviderRouter,
    RoutingContext,
    ScoreAggregate,
    ScoreAggregateProvider,
    ScoreAggregateQuery,
    ScoreBasedPolicy,
    SQLiteMetricsStore,
)
from nygen_router.metrics import MetricsEvent
from nygen_router.storage.score_aggregation import validate_score_aggregates


def _provider(provider_id: str) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=f"Provider {provider_id}",
        protocol=ApiProtocol.OPENAI_CHAT,
        model=f"model-{provider_id}",
        base_url=f"https://{provider_id}.example.com/v1",
        api_key="secret",
    )


def _query(*provider_ids: str) -> ScoreAggregateQuery:
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    return ScoreAggregateQuery(
        providers=tuple(
            ScoreAggregateProvider(
                provider_id=provider_id,
                model=f"model-{provider_id}",
                protocol=ApiProtocol.OPENAI_CHAT,
            )
            for provider_id in provider_ids
        ),
        metrics_scope="scope-a",
        call_type=CallType.REGULAR,
        since=now - timedelta(hours=24),
        reference_time=now,
        weighting=FlatScoreWeighting(),
    )


def _zero(provider_id: str) -> ScoreAggregate:
    return ScoreAggregate(
        provider_id=provider_id,
        attempt_weight=0.0,
        success_weight=0.0,
        successful_latency_weight=0.0,
        successful_latency_total_ms=0.0,
        recent_error_count=0,
        rate_limit_count=0,
        timeout_count=0,
    )


class _AggregateStore:
    def __init__(self, aggregates: list[ScoreAggregate] | Exception) -> None:
        self.aggregates = aggregates
        self.aggregate_queries: list[ScoreAggregateQuery] = []
        self.raw_query_calls = 0

    def record_attempt(self, event: MetricsEvent) -> None:
        return None

    def query_recent(self, **kwargs: Any) -> list[MetricsEvent]:
        self.raw_query_calls += 1
        raise AssertionError("ScoreBasedPolicy must not query raw history")

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        self.aggregate_queries.append(query)
        if isinstance(self.aggregates, Exception):
            raise self.aggregates
        return list(self.aggregates)


class _DuplicateTieBreak:
    def __init__(self) -> None:
        self.calls = 0
        self.result: list[ProviderConfig] | None = None

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        self.calls += 1
        self.result = [eligible[1], eligible[0], eligible[1]]
        return self.result


def test_query_models_normalize_time_copy_providers_and_validate_weighting() -> None:
    provider = ScoreAggregateProvider(
        provider_id=" provider-a ",
        model=" model-a ",
        protocol=ApiProtocol.OPENAI_CHAT,
    )
    providers = [provider]
    plus_two = datetime(2026, 8, 6, 14, tzinfo=timezone(timedelta(hours=2)))
    query = ScoreAggregateQuery(
        providers=providers,  # type: ignore[arg-type] -- runtime defensive-copy contract
        metrics_scope=" scope-a ",
        call_type=CallType.REGULAR,
        since=plus_two - timedelta(hours=1),
        reference_time=plus_two,
        weighting=ExponentialScoreWeighting(half_life_hours=2),
    )
    providers.clear()

    assert query.providers == (provider,)
    assert query.providers[0].provider_id == "provider-a"
    assert query.providers[0].model == "model-a"
    assert query.metrics_scope == "scope-a"
    assert query.since.utcoffset() == timedelta(0)
    assert query.reference_time.utcoffset() == timedelta(0)
    assert query.weighting.half_life_hours == 2.0

    with pytest.raises(ValueError, match="distinct"):
        replace(query, providers=(provider, provider))
    with pytest.raises(ValueError, match="finite"):
        ExponentialScoreWeighting(float("nan"))
    with pytest.raises(ValueError, match="positive"):
        ExponentialScoreWeighting(0.0)


def test_score_policy_captures_now_once_queries_once_and_preserves_duplicates() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    now_calls = 0

    def captured_now() -> datetime:
        nonlocal now_calls
        now_calls += 1
        return now

    a, b = _provider("a"), _provider("b")
    tie_break = _DuplicateTieBreak()
    store = _AggregateStore([_zero("b"), _zero("a")])
    policy = ScoreBasedPolicy(tie_break_policy=tie_break, now=captured_now)
    context = RoutingContext(
        metrics_store=store,
        metrics_scope="scope-a",
        call_type=CallType.REGULAR,
    )

    ordered = policy.order([a, b], context)

    assert ordered is not tie_break.result
    assert ordered == tie_break.result
    assert tie_break.calls == 1
    assert now_calls == 1
    assert len(store.aggregate_queries) == 1
    query = store.aggregate_queries[0]
    assert [provider.provider_id for provider in query.providers] == ["b", "a"]
    assert [provider.model for provider in query.providers] == ["model-b", "model-a"]
    assert all(provider.protocol is ApiProtocol.OPENAI_CHAT for provider in query.providers)
    assert query.metrics_scope == "scope-a"
    assert query.call_type is CallType.REGULAR
    assert query.reference_time == now
    assert query.since == now - timedelta(hours=336)
    assert isinstance(query.weighting, FlatScoreWeighting)
    assert store.raw_query_calls == 0


def test_score_policy_invalid_complete_read_returns_exact_baseline_without_raw_fallback() -> None:
    a, b = _provider("a"), _provider("b")
    tie_break = _DuplicateTieBreak()
    store = _AggregateStore([_zero("b")])
    policy = ScoreBasedPolicy(
        tie_break_policy=tie_break,
        now=lambda: datetime(2026, 8, 6, 12, tzinfo=UTC),
    )
    context = RoutingContext(
        metrics_store=store,
        metrics_scope="scope-a",
        call_type=CallType.REGULAR,
    )

    result = policy.order([a, b], context)

    assert result is tie_break.result
    assert store.raw_query_calls == 0


def test_score_policy_maps_valid_totals_to_shared_stats_and_scores_every_occurrence() -> None:
    a, b = _provider("a"), _provider("b")
    tie_break = _DuplicateTieBreak()
    store = _AggregateStore(
        [
            replace(
                _zero("a"),
                attempt_weight=10.0,
                success_weight=10.0,
                successful_latency_weight=10.0,
                successful_latency_total_ms=100.0,
            ),
            replace(_zero("b"), attempt_weight=10.0, recent_error_count=10),
        ]
    )
    policy = ScoreBasedPolicy(
        tie_break_policy=tie_break,
        now=lambda: datetime(2026, 8, 6, 12, tzinfo=UTC),
    )

    ordered = policy.order(
        [a, b],
        RoutingContext(
            metrics_store=store,
            metrics_scope="scope-a",
            call_type=CallType.REGULAR,
        ),
    )

    assert [provider.provider_id for provider in ordered] == ["a", "b", "b"]
    assert len(store.aggregate_queries) == 1
    assert store.raw_query_calls == 0


def test_score_policy_all_scope_omits_only_scope_filter_and_deduplicates_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, b = _provider("a"), _provider("b")
    tie_break = _DuplicateTieBreak()
    store = _AggregateStore(RuntimeError("aggregate storage unavailable"))
    policy = ScoreBasedPolicy(
        history_scope=HistoryScope.ALL,
        tie_break_policy=tie_break,
        now=lambda: datetime(2026, 8, 6, 12, tzinfo=UTC),
    )
    context = RoutingContext(
        metrics_store=store,
        metrics_scope="scope-a",
        call_type=CallType.REGULAR,
    )

    with caplog.at_level(logging.DEBUG, logger="nygen_router.policies.score_based"):
        first = policy.order([a, b], context)
        second = policy.order([a, b], context)

    assert first is not second
    assert first == second == [b, a, b]
    assert [query.metrics_scope for query in store.aggregate_queries] == [None, None]
    assert store.raw_query_calls == 0
    history_logs = [
        record
        for record in caplog.records
        if "Metrics history is unavailable" in record.getMessage()
    ]
    assert [record.levelno for record in history_logs] == [logging.WARNING, logging.DEBUG]


@pytest.mark.parametrize(
    "invalid",
    [
        replace(_zero("a"), attempt_weight=-1.0),
        replace(_zero("a"), attempt_weight=float("nan")),
        replace(_zero("a"), success_weight=float("inf")),
        replace(_zero("a"), attempt_weight=1.0, success_weight=1.1),
        replace(_zero("a"), success_weight=1.0, successful_latency_weight=1.1),
        replace(_zero("a"), recent_error_count=1.5),  # type: ignore[arg-type]
        replace(_zero("a"), timeout_count=True),  # type: ignore[arg-type]
    ],
)
def test_complete_result_validation_rejects_malformed_totals(invalid: ScoreAggregate) -> None:
    with pytest.raises((TypeError, ValueError)):
        validate_score_aggregates(_query("a"), [invalid])


def test_complete_result_validation_rejects_missing_duplicate_and_unexpected_rows() -> None:
    query = _query("a", "b")
    invalid_sets = (
        [_zero("a")],
        [_zero("a"), _zero("a")],
        [_zero("a"), _zero("unexpected")],
    )

    for invalid in invalid_sets:
        with pytest.raises(ValueError):
            validate_score_aggregates(query, invalid)


def test_complete_result_validation_rejects_non_domain_rows_and_non_iterables() -> None:
    query = _query("a")

    with pytest.raises(TypeError, match="ScoreAggregate"):
        validate_score_aggregates(query, [("a", 0.0)])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="iterable"):
        validate_score_aggregates(query, None)  # type: ignore[arg-type]


class _LegacyStore:
    def record_attempt(self, event: MetricsEvent) -> None:
        return None

    def query_recent(self, **kwargs: Any) -> list[MetricsEvent]:
        return []


class _NonCallableAggregateStore(_LegacyStore):
    query_score_aggregates = None


@pytest.mark.parametrize(
    "invalid_store",
    [
        "metrics.sqlite",
        _LegacyStore(),
        _NonCallableAggregateStore(),
    ],
)
def test_router_rejects_incomplete_metrics_stores_at_construction(invalid_store: object) -> None:
    with pytest.raises(ConfigError, match="query_score_aggregates"):
        ProviderRouter(
            providers=[_provider("a")],
            metrics_scope="scope-a",
            metrics_store=invalid_store,  # type: ignore[arg-type]
        )


def test_router_accepts_none_and_both_complete_bundled_stores(tmp_path: Path) -> None:
    ProviderRouter(
        providers=[_provider("a")],
        metrics_scope="scope-a",
        metrics_store=None,
    )
    ProviderRouter(
        providers=[_provider("a")],
        metrics_scope="scope-a",
        metrics_store=SQLiteMetricsStore(tmp_path / "metrics.sqlite"),
    )
    ProviderRouter(
        providers=[_provider("a")],
        metrics_scope="scope-a",
        metrics_store=DuckDBMetricsStore(
            tmp_path / "metrics.duckdb",
            sdk_available=True,
        ),
    )
