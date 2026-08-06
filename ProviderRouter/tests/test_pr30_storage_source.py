from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nygen_router import (
    METRICS_SCHEMA_VERSION,
    ApiProtocol,
    CallType,
    DuckDBMetricsStore,
    ErrorCategory,
    ExponentialScoreWeighting,
    FlatScoreWeighting,
    MetricsEvent,
    MetricsSchemaMismatchError,
    ProviderConfig,
    ProviderStats,
    ScoreAggregateProvider,
    ScoreAggregateQuery,
    ScoreWeights,
    SQLiteMetricsStore,
    aggregate_stats,
    calculate_provider_score,
)
from nygen_router.cli import main
from nygen_router.storage.admin import (
    LocalBackend,
    StorageCompatibilityError,
    create_database,
    inspect_database,
    migrate_database,
)
from nygen_router.storage.schema import (
    CREATE_PROVIDER_ATTEMPTS_TABLE_SQL,
    CREATE_SCHEMA_VERSIONS_TABLE_SQL,
    DUCKDB_REQUIRED_METRICS_INDEXES,
    INSERT_METRICS_VERSION_SQL,
    METRICS_COMPONENT,
    SQLITE_REQUIRED_METRICS_INDEXES,
    SchemaState,
)
from nygen_router.storage.score_aggregation import provider_stats_from_score_aggregate

_DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None
Store = DuckDBMetricsStore | SQLiteMetricsStore
StoreFactory = Callable[[Path], Store]


def _sqlite_store(path: Path) -> Store:
    return SQLiteMetricsStore(path)


def _duckdb_store(path: Path) -> Store:
    return DuckDBMetricsStore(path)


@pytest.fixture(
    params=[
        pytest.param((LocalBackend.SQLITE, _sqlite_store, ".sqlite"), id="sqlite"),
        pytest.param(
            (LocalBackend.DUCKDB, _duckdb_store, ".duckdb"),
            id="duckdb",
            marks=pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed"),
        ),
    ]
)
def backend_case(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[LocalBackend, StoreFactory, Path]:
    backend, factory, suffix = request.param
    return backend, factory, tmp_path / f"metrics{suffix}"


def _event(
    *,
    timestamp: datetime,
    metrics_scope: str = "scope-a",
    provider_id: str = "provider-a",
    model: str = "model-a",
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
    call_type: CallType = CallType.REGULAR,
    success: bool = True,
    latency_ms: float | None = 100.0,
    error_type: str | None = None,
) -> MetricsEvent:
    return MetricsEvent(
        timestamp=timestamp,
        metrics_scope=metrics_scope,
        provider_id=provider_id,
        provider_name=f"Current {provider_id}",
        model=model,
        protocol=protocol,
        call_type=call_type,
        success=success,
        latency_ms=latency_ms,
        error_type=error_type,
    )


def _provider(
    provider_id: str,
    *,
    model: str | None = None,
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=f"Current {provider_id}",
        model=f"model-{provider_id}" if model is None else model,
        protocol=protocol,
        base_url=f"https://{provider_id}.example.com/v1",
        api_key="secret",
    )


def _query(
    now: datetime,
    *,
    metrics_scope: str | None = "scope-a",
    weighting: FlatScoreWeighting | ExponentialScoreWeighting | None = None,
) -> ScoreAggregateQuery:
    return ScoreAggregateQuery(
        providers=(
            ScoreAggregateProvider(
                provider_id="provider-a",
                model="model-a",
                protocol=ApiProtocol.OPENAI_CHAT,
            ),
            ScoreAggregateProvider(
                provider_id="provider-b",
                model="model-b",
                protocol=ApiProtocol.OPENAI_CHAT,
            ),
        ),
        metrics_scope=metrics_scope,
        call_type=CallType.REGULAR,
        since=now - timedelta(hours=6),
        reference_time=now,
        weighting=FlatScoreWeighting() if weighting is None else weighting,
    )


def _seed_partition_cases(store: Store, now: datetime) -> None:
    events = [
        _event(timestamp=now, success=True, latency_ms=100.0),
        _event(timestamp=now - timedelta(hours=1), success=True, latency_ms=None),
        _event(
            timestamp=now - timedelta(hours=2),
            success=False,
            latency_ms=1.0,
            error_type="timeout",
        ),
        _event(
            timestamp=now - timedelta(hours=3),
            metrics_scope="scope-b",
            success=False,
            error_type="rate_limit",
        ),
        _event(timestamp=now, model="wrong-model", success=True, latency_ms=1.0),
        _event(
            timestamp=now,
            protocol=ApiProtocol.OPENAI_RESPONSES,
            success=True,
            latency_ms=1.0,
        ),
        _event(
            timestamp=now,
            call_type=CallType.STREAMING,
            success=True,
            latency_ms=1.0,
        ),
        _event(timestamp=now - timedelta(hours=7), success=True, latency_ms=1.0),
    ]
    for event in events:
        store.record_attempt(event)


def test_flat_aggregate_has_exact_partitions_zero_rows_and_bounded_cardinality(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    _, factory, path = backend_case
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    store = factory(path)
    _seed_partition_cases(store, now)

    current = store.query_score_aggregates(_query(now))
    all_scopes = store.query_score_aggregates(_query(now, metrics_scope=None))
    store.close()

    assert len(current) == 2
    assert current[0].provider_id == "provider-a"
    assert current[0].attempt_weight == pytest.approx(3.0)
    assert current[0].success_weight == pytest.approx(2.0)
    assert current[0].successful_latency_weight == pytest.approx(1.0)
    assert current[0].successful_latency_total_ms == pytest.approx(100.0)
    assert current[0].recent_error_count == 1
    assert current[0].rate_limit_count == 0
    assert current[0].timeout_count == 1
    assert current[1].provider_id == "provider-b"
    assert current[1].attempt_weight == 0.0
    assert current[1].success_weight == 0.0
    assert current[1].successful_latency_weight == 0.0
    assert current[1].successful_latency_total_ms == 0.0
    assert current[1].recent_error_count == 0

    assert len(all_scopes) == 2
    assert all_scopes[0].attempt_weight == pytest.approx(4.0)
    assert all_scopes[0].success_weight == pytest.approx(2.0)
    assert all_scopes[0].recent_error_count == 2
    assert all_scopes[0].rate_limit_count == 1
    assert all_scopes[0].timeout_count == 1


def test_exponential_aggregate_uses_reference_time_and_exact_unweighted_tallies(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    _, factory, path = backend_case
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    store = factory(path)
    _seed_partition_cases(store, now)

    result = store.query_score_aggregates(
        _query(now, weighting=ExponentialScoreWeighting(half_life_hours=1.0))
    )
    store.close()

    assert len(result) == 2
    assert result[0].attempt_weight == pytest.approx(1.0 + 0.5 + 0.25, rel=1e-9)
    assert result[0].success_weight == pytest.approx(1.0 + 0.5, rel=1e-9)
    assert result[0].successful_latency_weight == pytest.approx(1.0, rel=1e-9)
    assert result[0].successful_latency_total_ms == pytest.approx(100.0, rel=1e-9)
    assert result[0].recent_error_count == 1
    assert result[0].timeout_count == 1


def test_fresh_schema_is_version_two_with_required_indexes_and_private_plan(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    backend, factory, path = backend_case
    create_database(backend, path)
    inspection = inspect_database(backend, path)
    store = factory(path)
    query = _query(datetime(2026, 8, 6, 12, tzinfo=UTC))

    plan = store._explain_score_aggregates(query)  # type: ignore[attr-defined]
    all_scope_plan = store._explain_score_aggregates(  # type: ignore[attr-defined]
        _query(datetime(2026, 8, 6, 12, tzinfo=UTC), metrics_scope=None)
    )
    store.close()

    assert METRICS_SCHEMA_VERSION == 2
    assert inspection.schema.state is SchemaState.CURRENT
    assert inspection.schema.metrics_version == 2
    assert plan
    assert all_scope_plan
    if backend is LocalBackend.SQLITE:
        assert len(SQLITE_REQUIRED_METRICS_INDEXES) == 1
        required_name = SQLITE_REQUIRED_METRICS_INDEXES[0].name
        assert required_name in "\n".join(plan)
        assert required_name in "\n".join(all_scope_plan)
    else:
        assert DUCKDB_REQUIRED_METRICS_INDEXES == ()
        assert "JOIN" in "\n".join(plan)
        assert "GROUP" in "\n".join(plan)


def _execute(
    backend: LocalBackend,
    path: Path,
    statements: list[tuple[str, tuple[object, ...]]],
) -> None:
    if backend is LocalBackend.SQLITE:
        connection: Any = sqlite3.connect(str(path))
    else:
        import duckdb

        connection = duckdb.connect(str(path))
    try:
        for sql, params in statements:
            connection.execute(sql, params)
        if backend is LocalBackend.SQLITE:
            connection.commit()
    finally:
        connection.close()


def test_missing_required_index_is_rejected_read_only(
    tmp_path: Path,
) -> None:
    backend = LocalBackend.SQLITE
    factory = _sqlite_store
    path = tmp_path / "missing-index.sqlite"
    create_database(backend, path)
    missing = SQLITE_REQUIRED_METRICS_INDEXES[0]
    _execute(backend, path, [(f"DROP INDEX {missing.name}", ())])
    before = path.read_bytes()

    inspection = inspect_database(backend, path)
    with pytest.raises(MetricsSchemaMismatchError, match="required project-owned indexes"):
        factory(path).query_score_aggregates(_query(datetime(2026, 8, 6, 12, tzinfo=UTC)))

    assert inspection.schema.state is SchemaState.UNKNOWN
    assert missing.name in inspection.schema.detail
    assert path.read_bytes() == before


@pytest.mark.parametrize("implicit", [False, True], ids=["versioned-v1", "implicit-v1"])
def test_version_one_has_no_runtime_or_administration_migration_route(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
    implicit: bool,
) -> None:
    backend, factory, path = backend_case
    statements = [(CREATE_PROVIDER_ATTEMPTS_TABLE_SQL, ())]
    if not implicit:
        statements.extend(
            [
                (CREATE_SCHEMA_VERSIONS_TABLE_SQL, ()),
                (INSERT_METRICS_VERSION_SQL, (METRICS_COMPONENT, 1)),
            ]
        )
    _execute(backend, path, statements)
    before = path.read_bytes()

    inspection = inspect_database(backend, path)
    expected_state = SchemaState.IMPLICIT_BASELINE if implicit else SchemaState.SUPPORTED_OLDER
    assert inspection.schema.state is expected_state
    assert inspection.schema.metrics_version == 1
    with pytest.raises(MetricsSchemaMismatchError, match="expected metrics=2"):
        factory(path).query_recent(since=datetime(2026, 8, 6, tzinfo=UTC))
    with pytest.raises(StorageCompatibilityError, match="no approved route"):
        migrate_database(backend, path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "weighting",
    [FlatScoreWeighting(), ExponentialScoreWeighting(half_life_hours=2.0)],
    ids=["flat", "exponential"],
)
@pytest.mark.parametrize(
    "call_type",
    [CallType.REGULAR, CallType.STREAMING],
    ids=["regular", "streaming"],
)
def test_sql_aggregates_match_the_python_oracle_for_rich_history(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
    weighting: FlatScoreWeighting | ExponentialScoreWeighting,
    call_type: CallType,
) -> None:
    _, factory, path = backend_case
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    providers = [_provider("provider-a"), _provider("provider-b"), _provider("provider-c")]
    events = [
        _event(
            timestamp=now - timedelta(hours=1),
            call_type=CallType.REGULAR,
            success=True,
            latency_ms=120.0,
        ),
        _event(
            timestamp=now - timedelta(hours=2),
            call_type=CallType.REGULAR,
            success=True,
            latency_ms=None,
        ),
        _event(
            timestamp=now - timedelta(hours=3),
            call_type=CallType.REGULAR,
            success=False,
            latency_ms=1.0,
            error_type=ErrorCategory.RATE_LIMIT.value,
        ),
        _event(
            timestamp=now - timedelta(minutes=30),
            call_type=CallType.STREAMING,
            success=True,
            latency_ms=25.0,
        ),
        _event(
            timestamp=now - timedelta(hours=1),
            call_type=CallType.STREAMING,
            success=True,
            latency_ms=None,
        ),
        _event(
            timestamp=now - timedelta(hours=2),
            call_type=CallType.STREAMING,
            success=False,
            latency_ms=2.0,
            error_type=ErrorCategory.TIMEOUT.value,
        ),
        _event(
            timestamp=now - timedelta(hours=1),
            provider_id="provider-b",
            model="model-provider-b",
            call_type=CallType.REGULAR,
            success=False,
            latency_ms=3.0,
            error_type=ErrorCategory.TIMEOUT.value,
        ),
        _event(
            timestamp=now - timedelta(hours=1),
            provider_id="provider-b",
            model="model-provider-b",
            call_type=CallType.STREAMING,
            success=False,
            latency_ms=None,
            error_type=ErrorCategory.RATE_LIMIT.value,
        ),
        _event(timestamp=now, metrics_scope="other-scope", success=True, latency_ms=1.0),
        _event(timestamp=now, model="wrong-model", success=True, latency_ms=1.0),
        _event(
            timestamp=now,
            protocol=ApiProtocol.OPENAI_RESPONSES,
            success=True,
            latency_ms=1.0,
        ),
        _event(timestamp=now - timedelta(hours=13), success=True, latency_ms=1.0),
    ]
    store = factory(path)
    for event in events:
        store.record_attempt(event)
    query = ScoreAggregateQuery(
        providers=tuple(
            ScoreAggregateProvider(
                provider_id=provider.provider_id,
                model=provider.model,
                protocol=provider.protocol,
            )
            for provider in providers
        ),
        metrics_scope="scope-a",
        call_type=call_type,
        since=now - timedelta(hours=12),
        reference_time=now,
        weighting=weighting,
    )

    actual_aggregates = store.query_score_aggregates(query)
    raw_events = store.query_recent(since=query.since, metrics_scope=query.metrics_scope)
    weight_fn: Callable[[MetricsEvent], float] | None = None
    if isinstance(weighting, ExponentialScoreWeighting):
        half_life_hours = weighting.half_life_hours

        def exponential_weight(event: MetricsEvent) -> float:
            age_hours = (now - event.timestamp).total_seconds() / 3600.0
            return 0.5 ** (age_hours / half_life_hours)

        weight_fn = exponential_weight
    expected_stats = aggregate_stats(raw_events, providers, call_type, weight_fn=weight_fn)
    actual_stats = {
        provider.provider_id: provider_stats_from_score_aggregate(
            aggregate,
            provider,
            call_type,
        )
        for aggregate, provider in zip(actual_aggregates, providers, strict=True)
    }
    store.close()

    assert [aggregate.provider_id for aggregate in actual_aggregates] == [
        "provider-a",
        "provider-b",
        "provider-c",
    ]
    for provider_id in expected_stats:
        _assert_stats_equivalent(actual_stats[provider_id], expected_stats[provider_id])
    failure_only = actual_aggregates[1]
    assert failure_only.attempt_weight > 0
    assert failure_only.success_weight == 0
    assert failure_only.successful_latency_weight == 0
    assert failure_only.successful_latency_total_ms == 0
    assert actual_aggregates[2].attempt_weight == 0


def test_zero_history_success_and_failure_self_correct_the_optimistic_score(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    _, factory, path = backend_case
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    provider = _provider("provider-a", model="model-a")
    store = factory(path)
    query = ScoreAggregateQuery(
        providers=(
            ScoreAggregateProvider(
                provider_id=provider.provider_id,
                model=provider.model,
                protocol=provider.protocol,
            ),
        ),
        metrics_scope="scope-a",
        call_type=CallType.REGULAR,
        since=now - timedelta(hours=24),
        reference_time=now,
        weighting=FlatScoreWeighting(),
    )
    weights = ScoreWeights(success_weight=1.0, speed_weight=0.0)

    def score() -> float:
        aggregate = store.query_score_aggregates(query)[0]
        stats = provider_stats_from_score_aggregate(aggregate, provider, CallType.REGULAR)
        return calculate_provider_score(stats, weights, call_type=CallType.REGULAR).total

    initial = score()
    store.record_attempt(_event(timestamp=now, success=True, latency_ms=50.0))
    after_success = score()
    store.record_attempt(
        _event(
            timestamp=now,
            success=False,
            latency_ms=1.0,
            error_type=ErrorCategory.SERVER_ERROR.value,
        )
    )
    after_failure = score()
    store.close()

    assert initial == weights.optimistic_start
    assert after_success > initial
    assert after_failure < after_success


def test_result_cardinality_depends_only_on_requested_providers(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    _, factory, path = backend_case
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    query = ScoreAggregateQuery(
        providers=tuple(
            ScoreAggregateProvider(
                provider_id=f"provider-{index}",
                model=f"model-{index}",
                protocol=ApiProtocol.OPENAI_CHAT,
            )
            for index in range(5)
        ),
        metrics_scope="scope-a",
        call_type=CallType.REGULAR,
        since=now - timedelta(hours=1),
        reference_time=now,
        weighting=FlatScoreWeighting(),
    )
    store = factory(path)
    empty = store.query_score_aggregates(query)
    for _ in range(120):
        store.record_attempt(
            _event(
                timestamp=now,
                provider_id="provider-0",
                model="model-0",
                success=True,
                latency_ms=10.0,
            )
        )
    populated = store.query_score_aggregates(query)
    store.close()

    expected_ids = [f"provider-{index}" for index in range(5)]
    assert [row.provider_id for row in empty] == expected_ids
    assert [row.provider_id for row in populated] == expected_ids
    assert len(empty) == len(populated) == 5
    assert populated[0].attempt_weight == 120


def test_version_two_reopen_supports_read_write_and_aggregate(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    _, factory, path = backend_case
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    first = factory(path)
    first.record_attempt(_event(timestamp=now, success=True, latency_ms=10.0))
    first.close()

    second = factory(path)
    assert len(second.query_recent(since=now - timedelta(seconds=1))) == 1
    second.record_attempt(
        _event(
            timestamp=now,
            success=False,
            latency_ms=None,
            error_type=ErrorCategory.TIMEOUT.value,
        )
    )
    aggregates = second.query_score_aggregates(_query(now))
    second.close()

    assert aggregates[0].attempt_weight == 2
    assert aggregates[0].success_weight == 1
    assert aggregates[0].timeout_count == 1


def test_malformed_required_index_is_rejected_read_only(
    tmp_path: Path,
) -> None:
    backend = LocalBackend.SQLITE
    factory = _sqlite_store
    path = tmp_path / "malformed-index.sqlite"
    create_database(backend, path)
    malformed = SQLITE_REQUIRED_METRICS_INDEXES[0]
    _execute(
        backend,
        path,
        [
            (f"DROP INDEX {malformed.name}", ()),
            (
                f"CREATE INDEX {malformed.name} ON provider_attempts (provider_id, timestamp)",
                (),
            ),
        ],
    )
    before = path.read_bytes()

    inspection = inspect_database(backend, path)
    store = factory(path)
    with pytest.raises(MetricsSchemaMismatchError, match="required project-owned indexes"):
        store.query_score_aggregates(_query(datetime(2026, 8, 6, 12, tzinfo=UTC)))
    store.close()

    assert inspection.schema.state is SchemaState.UNKNOWN
    assert malformed.name in inspection.schema.detail
    assert "columns=" in inspection.schema.detail
    assert path.read_bytes() == before


def test_direct_aggregate_request_errors_are_not_swallowed(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    _, factory, path = backend_case
    store = factory(path)

    with pytest.raises(TypeError, match="ScoreAggregateQuery"):
        store.query_score_aggregates(object())  # type: ignore[arg-type]

    assert not path.exists()


def test_cli_refuses_version_one_without_creating_a_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "version-one.sqlite"
    backup = tmp_path / "backup.sqlite"
    _execute(
        LocalBackend.SQLITE,
        path,
        [
            (CREATE_PROVIDER_ATTEMPTS_TABLE_SQL, ()),
            (CREATE_SCHEMA_VERSIONS_TABLE_SQL, ()),
            (INSERT_METRICS_VERSION_SQL, (METRICS_COMPONENT, 1)),
        ],
    )
    before = path.read_bytes()

    result = main(
        [
            "storage",
            "migrate",
            "--backend",
            "sqlite",
            "--path",
            str(path),
            "--backup",
            str(backup),
        ]
    )
    captured = capsys.readouterr()

    assert result == 4
    assert captured.out == ""
    assert "no approved route" in captured.err
    assert path.read_bytes() == before
    assert not backup.exists()


def _assert_stats_equivalent(actual: ProviderStats, expected: ProviderStats) -> None:
    assert actual.provider_id == expected.provider_id
    assert actual.provider_name == expected.provider_name
    assert actual.recent_error_count == expected.recent_error_count
    assert actual.rate_limit_count == expected.rate_limit_count
    assert actual.timeout_count == expected.timeout_count
    for field_name in (
        "regular_attempt_count",
        "regular_success_count",
        "regular_success_rate",
        "regular_avg_latency_ms",
        "streaming_attempt_count",
        "streaming_success_count",
        "streaming_success_rate",
        "streaming_avg_ttft_ms",
    ):
        actual_value = getattr(actual, field_name)
        expected_value = getattr(expected, field_name)
        if expected_value is None:
            assert actual_value is None
        else:
            assert actual_value == pytest.approx(expected_value, rel=1e-8, abs=1e-12)
