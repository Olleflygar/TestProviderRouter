from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nygen_router import (
    ApiProtocol,
    CallType,
    DuckDBMetricsStore,
    ErrorCategory,
    ExponentialScoreWeighting,
    FlatScoreWeighting,
    ProviderConfig,
    ProviderStats,
    ScoreAggregate,
    ScoreAggregateProvider,
    ScoreAggregateQuery,
    SQLiteMetricsStore,
    aggregate_stats,
)
from nygen_router.metrics import MetricsEvent
from nygen_router.storage.admin import LocalBackend, create_database
from nygen_router.storage.base import INSERT_PROVIDER_ATTEMPT_SQL
from nygen_router.storage.schema import (
    DUCKDB_REQUIRED_METRICS_INDEXES,
    SQLITE_REQUIRED_METRICS_INDEXES,
    IndexDefinition,
)
from nygen_router.storage.score_aggregation import provider_stats_from_score_aggregate

MINIMUM_ROWS = 50_000
DEFAULT_ROWS = 60_000
DEFAULT_REPETITIONS = 7
PROVIDER_COUNT = 24
REQUESTED_PROVIDER_COUNT = 9
REFERENCE_TIME = datetime(2026, 8, 6, 12, tzinfo=UTC)
DATA_SPAN_SECONDS = 96 * 60 * 60
REL_TOLERANCE = 1e-9
ABS_TOLERANCE = 1e-9
SCOPES = ("scope-a", "scope-b", "scope-c")
PROTOCOLS = (ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES)
CALL_TYPES = (CallType.REGULAR, CallType.STREAMING)
SQLITE_INDEX_MODES = ("schema-required", "two-index-candidate", "unindexed")
DUCKDB_INDEX_MODES = ("schema-required", "two-index-candidate")
SCOPED_INDEX_CANDIDATE = IndexDefinition(
    name="provider_attempts_scope_partition_timestamp_idx",
    table="provider_attempts",
    unique=False,
    columns=(
        "metrics_scope",
        "provider_id",
        "model",
        "protocol",
        "call_type",
        "timestamp",
    ),
)
TWO_INDEX_CANDIDATE = (SCOPED_INDEX_CANDIDATE, SQLITE_REQUIRED_METRICS_INDEXES[0])

Store = DuckDBMetricsStore | SQLiteMetricsStore
Weighting = FlatScoreWeighting | ExponentialScoreWeighting
DatabaseRow = tuple[object, ...]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark PR30 score aggregation against real DuckDB and SQLite databases."
    )
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    args = parser.parse_args()
    if args.rows < MINIMUM_ROWS:
        parser.error(f"--rows must be at least {MINIMUM_ROWS}")
    if args.repetitions < 3:
        parser.error("--repetitions must be at least 3")
    return args


def _build_rows(row_count: int) -> list[DatabaseRow]:
    rows: list[DatabaseRow] = []
    error_categories = (
        ErrorCategory.RATE_LIMIT.value,
        ErrorCategory.TIMEOUT.value,
        ErrorCategory.SERVER_ERROR.value,
        ErrorCategory.CONNECTION.value,
    )
    for row_number in range(row_count):
        provider_number = row_number % PROVIDER_COUNT
        provider_id = f"provider-{provider_number:02d}"
        timestamp = REFERENCE_TIME - timedelta(seconds=(row_number * 37) % DATA_SPAN_SECONDS)
        metrics_scope = SCOPES[(row_number // 5) % len(SCOPES)]
        protocol = PROTOCOLS[(row_number // 7) % len(PROTOCOLS)]
        call_type = CALL_TYPES[(row_number // 13) % len(CALL_TYPES)]
        uses_alternate_model = (row_number // 11) % 5 == 0
        model_prefix = "alt-model" if uses_alternate_model else "model"
        success = (row_number * 17 + row_number // 19) % 7 not in {0, 1}
        if success:
            latency_ms = None if row_number % 11 == 0 else float(25 + (row_number * 29) % 800)
            error_type = None
        else:
            latency_ms = None if row_number % 3 else float(1 + row_number % 30)
            error_type = error_categories[row_number % len(error_categories)]
        stream_opened = None if call_type is CallType.REGULAR else row_number % 17 != 0
        total_duration_ms = (
            float(400 + row_number % 1_200)
            if latency_ms is None
            else latency_ms + float(100 + row_number % 300)
        )
        rows.append(
            (
                f"event-{row_number:07d}",
                timestamp.isoformat(),
                metrics_scope,
                provider_id,
                f"Historical provider {provider_number:02d}-{row_number % 3}",
                f"{model_prefix}-{provider_number:02d}",
                protocol.value,
                call_type.value,
                success,
                stream_opened,
                latency_ms,
                total_duration_ms,
                error_type,
            )
        )
    return rows


def _providers() -> list[ProviderConfig]:
    providers = [
        ProviderConfig(
            provider_id=f"provider-{provider_number:02d}",
            name=f"Current provider {provider_number:02d}",
            model=f"model-{provider_number:02d}",
            protocol=ApiProtocol.OPENAI_CHAT,
            base_url=f"https://provider-{provider_number:02d}.invalid/v1",
            api_key="benchmark-not-used",
        )
        for provider_number in range(REQUESTED_PROVIDER_COUNT - 1)
    ]
    providers.append(
        ProviderConfig(
            provider_id="provider-absent",
            name="Current absent provider",
            model="model-absent",
            protocol=ApiProtocol.OPENAI_CHAT,
            base_url="https://provider-absent.invalid/v1",
            api_key="benchmark-not-used",
        )
    )
    return providers


def _queries(providers: Sequence[ProviderConfig]) -> tuple[tuple[str, ScoreAggregateQuery], ...]:
    requested = tuple(
        ScoreAggregateProvider(
            provider_id=provider.provider_id,
            model=provider.model,
            protocol=provider.protocol,
        )
        for provider in providers
    )
    since = REFERENCE_TIME - timedelta(hours=36)
    return (
        (
            "current-scope-flat",
            ScoreAggregateQuery(
                providers=requested,
                metrics_scope="scope-a",
                call_type=CallType.REGULAR,
                since=since,
                reference_time=REFERENCE_TIME,
                weighting=FlatScoreWeighting(),
            ),
        ),
        (
            "all-scopes-exponential",
            ScoreAggregateQuery(
                providers=requested,
                metrics_scope=None,
                call_type=CallType.STREAMING,
                since=since,
                reference_time=REFERENCE_TIME,
                weighting=ExponentialScoreWeighting(half_life_hours=6.0),
            ),
        ),
    )


def _dataset_shape(rows: Sequence[DatabaseRow]) -> dict[str, object]:
    timestamps = [str(row[1]) for row in rows]
    error_counts = Counter(str(row[12]) for row in rows if row[12] is not None)
    success_count = sum(bool(row[8]) for row in rows)
    return {
        "rows": len(rows),
        "unique_ids": len({row[0] for row in rows}),
        "scopes": sorted({str(row[2]) for row in rows}),
        "providers": len({row[3] for row in rows}),
        "models": len({row[5] for row in rows}),
        "protocols": sorted({str(row[6]) for row in rows}),
        "call_types": sorted({str(row[7]) for row in rows}),
        "timestamp_min": min(timestamps),
        "timestamp_max": max(timestamps),
        "successes": success_count,
        "failures": len(rows) - success_count,
        "null_latencies": sum(row[10] is None for row in rows),
        "non_null_latencies": sum(row[10] is not None for row in rows),
        "error_categories": dict(sorted(error_counts.items())),
    }


def _seed_store(store: Store, backend: LocalBackend, rows: Sequence[DatabaseRow]) -> float:
    connection = getattr(store, "_connection", None)
    if connection is None:
        raise RuntimeError("store did not open its validated temporary database")
    started = time.perf_counter()
    if backend is LocalBackend.SQLITE:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(INSERT_PROVIDER_ATTEMPT_SQL, rows)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    else:
        try:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(
                """
                INSERT INTO provider_attempts
                WITH generated AS (
                    SELECT
                        n,
                        n % 24 AS provider_number,
                        (CAST(? AS TIMESTAMPTZ) AT TIME ZONE 'UTC')
                            - ((n * 37) % ?) * INTERVAL '1 second' AS event_time,
                        CASE floor(n / 5) % 3
                            WHEN 0 THEN 'scope-a'
                            WHEN 1 THEN 'scope-b'
                            ELSE 'scope-c'
                        END AS metrics_scope,
                        CASE floor(n / 7) % 2
                            WHEN 0 THEN 'openai_chat'
                            ELSE 'openai_responses'
                        END AS protocol,
                        CASE floor(n / 13) % 2
                            WHEN 0 THEN 'regular'
                            ELSE 'streaming'
                        END AS call_type,
                        floor(n / 11) % 5 = 0 AS uses_alternate_model,
                        (n * 17 + floor(n / 19)) % 7 NOT IN (0, 1) AS success
                    FROM range(?) AS generated_rows(n)
                ),
                with_latency AS (
                    SELECT
                        *,
                        CASE
                            WHEN success AND n % 11 = 0 THEN NULL
                            WHEN success THEN CAST(25 + (n * 29) % 800 AS DOUBLE)
                            WHEN n % 3 = 0 THEN CAST(1 + n % 30 AS DOUBLE)
                            ELSE NULL
                        END AS latency_ms
                    FROM generated
                )
                SELECT
                    printf('event-%07d', n),
                    strftime(event_time, '%Y-%m-%dT%H:%M:%S+00:00'),
                    metrics_scope,
                    printf('provider-%02d', provider_number),
                    printf(
                        'Historical provider %02d-%d',
                        provider_number,
                        n % 3
                    ),
                    printf(
                        '%s-%02d',
                        CASE
                            WHEN uses_alternate_model THEN 'alt-model'
                            ELSE 'model'
                        END,
                        provider_number
                    ),
                    protocol,
                    call_type,
                    success,
                    CASE
                        WHEN call_type = 'regular' THEN NULL
                        ELSE n % 17 != 0
                    END,
                    latency_ms,
                    CASE
                        WHEN latency_ms IS NULL THEN CAST(400 + n % 1200 AS DOUBLE)
                        ELSE latency_ms + CAST(100 + n % 300 AS DOUBLE)
                    END,
                    CASE
                        WHEN success THEN NULL
                        WHEN n % 4 = 0 THEN 'rate_limit'
                        WHEN n % 4 = 1 THEN 'timeout'
                        WHEN n % 4 = 2 THEN 'server_error'
                        ELSE 'connection'
                    END
                FROM with_latency
                """,
                [REFERENCE_TIME.isoformat(), DATA_SPAN_SECONDS, len(rows)],
            )
            connection.execute("COMMIT")
            connection.execute("CHECKPOINT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
    return time.perf_counter() - started


def _database_shape(
    backend: LocalBackend,
    path: Path,
    *,
    connection: Any | None = None,
) -> dict[str, object]:
    owns_connection = connection is None
    if connection is None:
        if backend is LocalBackend.SQLITE:
            connection = sqlite3.connect(str(path))
        else:
            import duckdb

            connection = duckdb.connect(str(path), read_only=True)
    try:
        row = connection.execute(
            """
            SELECT
                COUNT(*),
                COUNT(DISTINCT id),
                COUNT(DISTINCT provider_id),
                COUNT(DISTINCT model),
                MIN(timestamp),
                MAX(timestamp),
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN latency_ms IS NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN latency_ms IS NOT NULL THEN 1 ELSE 0 END)
            FROM provider_attempts
            """
        ).fetchone()
        assert row is not None
        error_counts = {
            str(error_type): int(count)
            for error_type, count in connection.execute(
                """
                SELECT error_type, COUNT(*)
                FROM provider_attempts
                WHERE error_type IS NOT NULL
                GROUP BY error_type
                ORDER BY error_type
                """
            ).fetchall()
        }

        def distinct_values(column: str) -> list[str]:
            return [
                str(value[0])
                for value in connection.execute(
                    f"SELECT DISTINCT {column} FROM provider_attempts ORDER BY {column}"
                ).fetchall()
            ]

        return {
            "rows": int(row[0]),
            "unique_ids": int(row[1]),
            "scopes": distinct_values("metrics_scope"),
            "providers": int(row[2]),
            "models": int(row[3]),
            "protocols": distinct_values("protocol"),
            "call_types": distinct_values("call_type"),
            "timestamp_min": str(row[4]),
            "timestamp_max": str(row[5]),
            "successes": int(row[6]),
            "failures": int(row[7]),
            "null_latencies": int(row[8]),
            "non_null_latencies": int(row[9]),
            "error_categories": error_counts,
        }
    finally:
        if owns_connection:
            connection.close()


def _open_store(backend: LocalBackend, path: Path) -> Store:
    if backend is LocalBackend.SQLITE:
        return SQLiteMetricsStore(path)
    return DuckDBMetricsStore(path)


def _required_indexes(backend: LocalBackend) -> tuple[IndexDefinition, ...]:
    if backend is LocalBackend.SQLITE:
        return SQLITE_REQUIRED_METRICS_INDEXES
    return DUCKDB_REQUIRED_METRICS_INDEXES


def _index_modes(backend: LocalBackend) -> tuple[str, ...]:
    if backend is LocalBackend.SQLITE:
        return SQLITE_INDEX_MODES
    return DUCKDB_INDEX_MODES


def _configure_index_mode(
    store: Store, backend: LocalBackend, mode: str
) -> tuple[tuple[str, ...], float]:
    store.query_recent(
        since=REFERENCE_TIME + timedelta(seconds=1),
        provider_id="provider-validation-only",
    )
    connection = getattr(store, "_connection", None)
    if connection is None:
        raise RuntimeError("store did not open its validated temporary database")
    required_indexes = _required_indexes(backend)
    required_names = {definition.name for definition in required_indexes}
    started = time.perf_counter()
    if mode == "two-index-candidate":
        for definition in TWO_INDEX_CANDIDATE:
            if definition.name not in required_names:
                connection.execute(definition.create_sql)
    elif mode == "unindexed":
        for definition in required_indexes:
            connection.execute(f"DROP INDEX {definition.name}")
        if backend is LocalBackend.SQLITE:
            connection.execute("PRAGMA automatic_index = OFF")
    elif mode != "schema-required":
        raise ValueError(f"unknown index mode {mode!r}")
    if backend is LocalBackend.SQLITE:
        connection.commit()
        rows = connection.execute("PRAGMA index_list('provider_attempts')").fetchall()
        indexes = tuple(
            sorted(
                str(row[1])
                for row in rows
                if str(row[1]) in {index.name for index in TWO_INDEX_CANDIDATE}
            )
        )
    else:
        rows = connection.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'provider_attempts' "
            "ORDER BY index_name"
        ).fetchall()
        indexes = tuple(
            str(row[0])
            for row in rows
            if str(row[0]) in {index.name for index in TWO_INDEX_CANDIDATE}
        )
    return indexes, time.perf_counter() - started


def _active_database_bytes(store: Store, backend: LocalBackend, path: Path) -> int:
    connection = getattr(store, "_connection", None)
    if connection is None:
        raise RuntimeError("store did not open its validated temporary database")
    if backend is LocalBackend.SQLITE:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        return (page_count - free_pages) * page_size
    connection.execute("CHECKPOINT")
    return path.stat().st_size


def _capture_plan(store: Store, query: ScoreAggregateQuery) -> tuple[str, ...]:
    explain = getattr(store, "_explain_score_aggregates", None)
    if not callable(explain):
        raise RuntimeError("bundled store does not expose its private benchmark plan seam")
    plan = tuple(explain(query))
    if not plan:
        raise AssertionError("query plan must not be empty")
    return plan


def _access_path(
    backend: LocalBackend,
    plan: Sequence[str],
    existing_indexes: Sequence[str],
) -> str:
    text = "\n".join(plan)
    if backend is LocalBackend.SQLITE:
        used = [index.name for index in TWO_INDEX_CANDIDATE if index.name in text]
        if used:
            return f"persistent SQLite index search using {', '.join(used)}"
        if "AUTOMATIC" in text.upper():
            return "SQLite automatic transient index"
        if "SCAN p" in text or "SCAN provider_attempts" in text:
            return "SQLite table scan (automatic indexes disabled)"
        return "SQLite plan did not identify a persistent project index"
    if "Index Scan" in text or "INDEX_SCAN" in text:
        return "DuckDB index scan"
    if "Sequential Scan" in text or "SEQ_SCAN" in text:
        suffix = (
            " despite persistent ART indexes being present"
            if existing_indexes
            else " with no project query indexes present"
        )
        return f"DuckDB sequential scan{suffix}"
    return "DuckDB plan did not expose a recognized scan label"


def _time_public_query(
    store: Store,
    query: ScoreAggregateQuery,
    repetitions: int,
) -> tuple[list[float], list[ScoreAggregate]]:
    warm_result = store.query_score_aggregates(query)
    _assert_result_cardinality(query, warm_result)
    durations_ms: list[float] = []
    last_result = warm_result
    for _ in range(repetitions):
        started = time.perf_counter()
        last_result = store.query_score_aggregates(query)
        durations_ms.append((time.perf_counter() - started) * 1_000.0)
        _assert_result_cardinality(query, last_result)
    return durations_ms, last_result


def _assert_result_cardinality(
    query: ScoreAggregateQuery, aggregates: Sequence[ScoreAggregate]
) -> None:
    if len(aggregates) != len(query.providers):
        raise AssertionError(
            f"aggregate returned {len(aggregates)} rows for {len(query.providers)} providers"
        )


def _oracle(
    store: Store,
    query: ScoreAggregateQuery,
    providers: Sequence[ProviderConfig],
) -> tuple[dict[str, ProviderStats], int, int]:
    raw_events = store.query_recent(since=query.since, metrics_scope=query.metrics_scope)
    provider_by_id = {provider.provider_id: provider for provider in providers}
    matching_rows = sum(
        event.call_type is query.call_type
        and event.provider_id in provider_by_id
        and event.model == provider_by_id[event.provider_id].model
        and event.protocol is provider_by_id[event.provider_id].protocol
        for event in raw_events
    )
    weight_fn: Callable[[MetricsEvent], float] | None = None
    if isinstance(query.weighting, ExponentialScoreWeighting):
        half_life_hours = query.weighting.half_life_hours

        def exponential_weight(event: MetricsEvent) -> float:
            age_hours = (query.reference_time - event.timestamp).total_seconds() / 3_600.0
            return 0.5 ** (age_hours / half_life_hours)

        weight_fn = exponential_weight
    return (
        aggregate_stats(raw_events, providers, query.call_type, weight_fn=weight_fn),
        len(raw_events),
        matching_rows,
    )


def _assert_oracle_equivalent(
    aggregates: Sequence[ScoreAggregate],
    expected: dict[str, ProviderStats],
    providers: Sequence[ProviderConfig],
    call_type: CallType,
) -> None:
    for aggregate, provider in zip(aggregates, providers, strict=True):
        actual = provider_stats_from_score_aggregate(aggregate, provider, call_type)
        wanted = expected[provider.provider_id]
        for field_name in (
            "provider_id",
            "provider_name",
            "recent_error_count",
            "rate_limit_count",
            "timeout_count",
        ):
            if getattr(actual, field_name) != getattr(wanted, field_name):
                raise AssertionError(
                    f"oracle mismatch for {provider.provider_id}.{field_name}: "
                    f"{getattr(actual, field_name)!r} != {getattr(wanted, field_name)!r}"
                )
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
            expected_value = getattr(wanted, field_name)
            if actual_value is None or expected_value is None:
                if actual_value is not expected_value:
                    raise AssertionError(
                        f"oracle mismatch for {provider.provider_id}.{field_name}: "
                        f"{actual_value!r} != {expected_value!r}"
                    )
            elif not math.isclose(
                actual_value,
                expected_value,
                rel_tol=REL_TOLERANCE,
                abs_tol=ABS_TOLERANCE,
            ):
                raise AssertionError(
                    f"oracle mismatch for {provider.provider_id}.{field_name}: "
                    f"{actual_value!r} != {expected_value!r}"
                )


def _print_plan(plan: Sequence[str]) -> None:
    print("plan_begin")
    for line in plan:
        for physical_line in line.splitlines():
            print(f"  {physical_line}")
    print("plan_end")


def _print_results(aggregates: Sequence[ScoreAggregate]) -> None:
    serializable = [
        {
            key: round(value, 12) if isinstance(value, float) else value
            for key, value in asdict(aggregate).items()
        }
        for aggregate in aggregates
    ]
    print(f"aggregate_results={json.dumps(serializable, sort_keys=True)}")


def _run_backend(
    backend: LocalBackend,
    root: Path,
    rows: Sequence[DatabaseRow],
    expected_shape: dict[str, object],
    providers: Sequence[ProviderConfig],
    queries: Sequence[tuple[str, ScoreAggregateQuery]],
    repetitions: int,
) -> None:
    suffix = ".sqlite" if backend is LocalBackend.SQLITE else ".duckdb"
    print(
        f"\nbackend={backend.value} schema_required_indexes="
        f"{[definition.name for definition in _required_indexes(backend)]}"
    )

    oracle_by_query: dict[str, dict[str, ProviderStats]] = {}
    oracle_counts: dict[str, tuple[int, int]] = {}
    for mode in _index_modes(backend):
        case_path = root / f"{backend.value}-{mode}{suffix}"
        create_database(backend, case_path)
        store = _open_store(backend, case_path)
        try:
            existing_indexes, index_setup_seconds = _configure_index_mode(store, backend, mode)
            seed_seconds = _seed_store(store, backend, rows)
            connection = getattr(store, "_connection", None)
            stored_shape = _database_shape(backend, case_path, connection=connection)
            if stored_shape != expected_shape:
                raise AssertionError(
                    f"{backend.value} stored dataset shape differs from generated logical rows: "
                    f"{stored_shape!r} != {expected_shape!r}"
                )
            active_bytes = _active_database_bytes(store, backend, case_path)
            print(
                f"\nsetup={mode} input_rows={stored_shape['rows']} "
                f"persistent_query_indexes={list(existing_indexes)} "
                f"index_setup_seconds={index_setup_seconds:.6f} "
                f"seed_seconds={seed_seconds:.6f} active_database_bytes={active_bytes} "
                f"seed_and_index_setup_excluded_from_query_timings=true"
            )
            for query_name, query in queries:
                plan = _capture_plan(store, query)
                access_path = _access_path(backend, plan, existing_indexes)
                timings_ms, aggregates = _time_public_query(store, query, repetitions)
                if mode == "schema-required":
                    expected, candidate_rows, matching_rows = _oracle(store, query, providers)
                    oracle_by_query[query_name] = expected
                    oracle_counts[query_name] = (candidate_rows, matching_rows)
                _assert_oracle_equivalent(
                    aggregates,
                    oracle_by_query[query_name],
                    providers,
                    query.call_type,
                )
                candidate_rows, matching_rows = oracle_counts[query_name]
                print(
                    f"query={query_name} requested_providers={len(query.providers)} "
                    f"returned_rows={len(aggregates)} candidate_input_rows={candidate_rows} "
                    f"exact_partition_input_rows={matching_rows}"
                )
                print(f"access_path={access_path}")
                _print_plan(plan)
                print(
                    "timings_ms="
                    f"{json.dumps([round(value, 6) for value in timings_ms])} "
                    f"repetitions={repetitions} median_ms={statistics.median(timings_ms):.6f}"
                )
                print(
                    f"oracle=PASS semantic_oracle=aggregate_stats "
                    f"rel_tolerance={REL_TOLERANCE} abs_tolerance={ABS_TOLERANCE}"
                )
                _print_results(aggregates)
        finally:
            store.close()


def main() -> int:
    args = _parse_args()
    import duckdb

    rows = _build_rows(args.rows)
    providers = _providers()
    queries = _queries(providers)
    shape = _dataset_shape(rows)
    if shape["rows"] != shape["unique_ids"] or int(shape["rows"]) < MINIMUM_ROWS:
        raise AssertionError("benchmark rows must be unique and meet the minimum cardinality")

    print("PR30 storage-side score aggregation benchmark")
    print(
        f"python_timestamp={datetime.now(UTC).isoformat()} sqlite_version={sqlite3.sqlite_version} "
        f"duckdb_version={duckdb.__version__}"
    )
    print(f"dataset_shape={json.dumps(shape, sort_keys=True)}")
    print(
        f"requested_provider_count={len(providers)} "
        f"requested_provider_ids={[provider.provider_id for provider in providers]}"
    )
    print(
        f"repetitions={args.repetitions} warmups_per_query=1 "
        f"timing_scope=public_query_score_aggregates_execute_and_fetch"
    )

    with tempfile.TemporaryDirectory(prefix="nygen-pr30-benchmark-") as temporary:
        root = Path(temporary)
        for backend in (LocalBackend.SQLITE, LocalBackend.DUCKDB):
            _run_backend(
                backend,
                root,
                rows,
                shape,
                providers,
                queries,
                args.repetitions,
            )
    print("\nbenchmark_status=PASS temporary_databases_cleaned=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
