"""Measure the PostgreSQL score-aggregate plan with and without the candidate index.

Indexes are earned, not assumed. This seeds a realistic attempt history into a
real PostgreSQL database, then captures the planner's chosen access path and
the wall-clock cost of the public aggregate call for the current-scope and
all-scope queries, with the candidate index present and absent.

The durable evidence is the access path and the bounded result cardinality.
Timings against a managed database are dominated by network round trips and
are reported only as supporting context, never as a latency promise.

Usage:

    cd ProviderRouter
    NYGEN_ROUTER_TEST_POSTGRES_URL=... \\
        .venv/bin/python benchmarks/pr14a_postgres_score_aggregation.py \\
        --rows 60000 --repetitions 7
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from llm_provider_router import ApiProtocol, CallType, ErrorCategory, PostgresMetricsStore
from llm_provider_router.metrics import MetricsEvent
from llm_provider_router.storage.schema import (
    POSTGRES_REQUIRED_METRICS_INDEXES,
    SCHEMA_VERSIONS_TABLE,
)
from llm_provider_router.storage.score_aggregation import (
    ExponentialScoreWeighting,
    FlatScoreWeighting,
    ScoreAggregateProvider,
    ScoreAggregateQuery,
)

DEFAULT_ROWS = 60_000
DEFAULT_REPETITIONS = 7
PROVIDER_COUNT = 24
REQUESTED_PROVIDERS = 9
SCOPES = ("app:prod", "app:staging", "batch:nightly")
MODELS = ("model-a", "model-b")
URL_ENV = "NYGEN_ROUTER_TEST_POSTGRES_URL"

BENCHMARK_CONFIG = {
    "connect_timeout_seconds": 15.0,
    "statement_timeout_seconds": 60.0,
    "checkout_timeout_seconds": 15.0,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--url", default=os.environ.get(URL_ENV))
    return parser.parse_args()


def _events(row_count: int, reference: datetime) -> list[MetricsEvent]:
    events: list[MetricsEvent] = []
    for index in range(row_count):
        provider = f"provider_{index % PROVIDER_COUNT:02d}"
        success = index % 5 != 0
        error = None
        if not success:
            error = (ErrorCategory.RATE_LIMIT, ErrorCategory.TIMEOUT, ErrorCategory.SERVER_ERROR)[
                index % 3
            ].value
        events.append(
            MetricsEvent(
                metrics_scope=SCOPES[index % len(SCOPES)],
                provider_id=provider,
                provider_name=provider,
                model=MODELS[index % len(MODELS)],
                protocol=ApiProtocol.OPENAI_CHAT,
                call_type=CallType.REGULAR if index % 4 else CallType.STREAMING,
                success=success,
                latency_ms=None if not success else 50.0 + (index % 400),
                error_type=error,
                timestamp=reference - timedelta(minutes=index % (60 * 24 * 14)),
            )
        )
    return events


def _queries(reference: datetime) -> tuple[tuple[str, ScoreAggregateQuery], ...]:
    providers = tuple(
        ScoreAggregateProvider(
            provider_id=f"provider_{index:02d}",
            model=MODELS[0],
            protocol=ApiProtocol.OPENAI_CHAT,
        )
        for index in range(REQUESTED_PROVIDERS)
    )
    common = {
        "providers": providers,
        "call_type": CallType.REGULAR,
        "reference_time": reference,
    }
    return (
        (
            "current-scope-flat",
            ScoreAggregateQuery(
                metrics_scope=SCOPES[0],
                since=reference - timedelta(hours=336),
                weighting=FlatScoreWeighting(),
                **common,
            ),
        ),
        (
            "all-scope-flat",
            ScoreAggregateQuery(
                metrics_scope=None,
                since=reference - timedelta(hours=336),
                weighting=FlatScoreWeighting(),
                **common,
            ),
        ),
        (
            "current-scope-exponential",
            ScoreAggregateQuery(
                metrics_scope=SCOPES[0],
                since=reference - timedelta(hours=72),
                weighting=ExponentialScoreWeighting(half_life_hours=12.0),
                **common,
            ),
        ),
    )


def _seed(store: PostgresMetricsStore, events: Sequence[MetricsEvent]) -> float:
    started = time.perf_counter()
    for start in range(0, len(events), 500):
        store.record_attempts(events[start : start + 500])
    return time.perf_counter() - started


def _reset(store: PostgresMetricsStore) -> None:
    from llm_provider_router.storage.admin import create_postgres_database

    with store._connection(validate=False) as connection:
        connection.execute("DROP TABLE IF EXISTS provider_attempts")
        connection.execute(f"DROP TABLE IF EXISTS {SCHEMA_VERSIONS_TABLE}")
    create_postgres_database(store._url)


def _set_indexes(store: PostgresMetricsStore, *, present: bool) -> None:
    with store._connection(validate=False) as connection:
        for definition in POSTGRES_REQUIRED_METRICS_INDEXES:
            if present:
                connection.execute(definition.create_sql)
            else:
                connection.execute(f"DROP INDEX IF EXISTS {definition.name}")


def _access_path(plan: Sequence[str]) -> str:
    text = " ".join(plan).lower()
    if "index scan" in text or "index only scan" in text or "bitmap index scan" in text:
        return "index"
    if "seq scan" in text:
        return "sequential"
    return "other"


def main() -> int:
    args = _parse_args()
    if not args.url:
        print(f"error: pass --url or set {URL_ENV}", file=sys.stderr)
        return 2

    reference = datetime.now(UTC)
    events = _events(args.rows, reference)
    store = PostgresMetricsStore(args.url, config=BENCHMARK_CONFIG)
    report: dict[str, object] = {
        "rows": args.rows,
        "repetitions": args.repetitions,
        "requested_providers": REQUESTED_PROVIDERS,
    }
    try:
        _reset(store)
        seed_seconds = _seed(store, events)
        report["seed_seconds"] = round(seed_seconds, 3)
        with store._connection(validate=False) as connection:
            (server_version,) = connection.execute("SHOW server_version").fetchone()
        report["server_version"] = server_version

        modes: dict[str, object] = {}
        for present in (False, True):
            _set_indexes(store, present=present)
            measurements: dict[str, object] = {}
            for name, query in _queries(reference):
                plan = store._explain_score_aggregates(query)
                timings = []
                for _ in range(args.repetitions):
                    started = time.perf_counter()
                    rows = store.query_score_aggregates(query)
                    timings.append((time.perf_counter() - started) * 1000.0)
                    if len(rows) != REQUESTED_PROVIDERS:
                        raise SystemExit(f"cardinality changed: {len(rows)} rows for {name}")
                measurements[name] = {
                    "access_path": _access_path(plan),
                    "median_ms": round(statistics.median(timings), 3),
                    "min_ms": round(min(timings), 3),
                    "returned_rows": REQUESTED_PROVIDERS,
                    "plan": list(plan),
                }
            modes["with_index" if present else "no_index"] = measurements
        report["modes"] = modes
    finally:
        store.close()

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
