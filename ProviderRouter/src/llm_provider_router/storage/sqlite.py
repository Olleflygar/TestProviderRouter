from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from llm_provider_router.config import ApiProtocol
from llm_provider_router.errors import ErrorCategory
from llm_provider_router.metrics import MetricsEvent
from llm_provider_router.storage.base import (
    INSERT_PROVIDER_ATTEMPT_SQL,
    build_query_recent_sql,
    event_to_params,
    row_to_event,
)
from llm_provider_router.storage.schema import (
    SCHEMA_VERSIONS_TABLE,
    SELECT_SCHEMA_VERSIONS_SQL,
    SQLITE_REQUIRED_METRICS_INDEXES,
    TABLE_INFO_PROVIDER_ATTEMPTS_SQL,
    TABLE_INFO_SCHEMA_VERSIONS_SQL,
    IndexDefinition,
    MetricsSchemaMismatchError,
    SchemaReport,
    inspect_schema_rows,
    validate_runtime_schema,
)
from llm_provider_router.storage.score_aggregation import (
    ExponentialScoreWeighting,
    ScoreAggregate,
    ScoreAggregateQuery,
    validate_score_aggregates,
)
from llm_provider_router.types import CallType


class SQLiteMetricsStore:
    """Stdlib sqlite3-backed MetricsStore -- no optional dependency required.

    The recommended option when several local processes must share one store:
    SQLite handles cross-process file locking natively, unlike DuckDB.
    Within one process the store is thread-safe: one lock serializes every
    database operation (connection creation, reads, writes, close) on the
    single shared connection, and is never held outside database work.
    Metrics v2 keeps raw query_recent for direct callers and executes one
    backend aggregate query for scoring through the single measured partition-
    and-timestamp index used by current- and all-scope plans.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        # Serializes all use of the single connection, including its lazy
        # creation and close: one database operation at a time from any
        # thread. Held only for database work, never around callers' other
        # activity.
        self._lock = threading.Lock()

    def record_attempt(self, event: MetricsEvent) -> None:
        params = event_to_params(event)
        with self._lock:
            connection = self._connect()
            connection.execute(INSERT_PROVIDER_ATTEMPT_SQL, params)
            connection.commit()

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
        query, params = build_query_recent_sql(
            since=since,
            metrics_scope=metrics_scope,
            provider_id=provider_id,
            model=model,
            protocol=protocol,
            call_type=call_type,
        )
        with self._lock:
            connection = self._connect()
            rows = connection.execute(query, params).fetchall()
        return [row_to_event(row) for row in rows]

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        """Return one validated intermediate-total row per requested provider."""
        sql, params = _build_score_aggregate_sql(query)
        with self._lock:
            rows = self._connect().execute(sql, params).fetchall()
        aggregates = [_row_to_score_aggregate(row) for row in rows]
        validated = validate_score_aggregates(query, aggregates)
        return [validated[provider.provider_id] for provider in query.providers]

    def _explain_score_aggregates(self, query: ScoreAggregateQuery) -> tuple[str, ...]:
        """Return private, engine-neutral plan text for deterministic validation."""
        sql, params = _build_score_aggregate_sql(query)
        with self._lock:
            rows = self._connect().execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        return tuple(" | ".join(str(value) for value in row) for row in rows)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _connect(self) -> sqlite3.Connection:
        # Callers hold self._lock; never call this without it.
        if self._connection is None:
            existed = self.path.exists()
            if existed:
                report = _inspect_existing(self.path)
                validate_runtime_schema(report, backend="SQLite", path=str(self.path.resolve()))
            else:
                from llm_provider_router.storage.admin import LocalBackend, create_database

                create_database(LocalBackend.SQLITE, self.path)
            # check_same_thread=False is safe only because self._lock
            # guarantees the connection is used by one thread at a time.
            connection = sqlite3.connect(str(self.path), check_same_thread=False)
            self._connection = connection
        return self._connection


def _inspect_existing(path: Path) -> SchemaReport:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise MetricsSchemaMismatchError(
            f"SQLite metrics database at {str(path.resolve())!r} could not be inspected safely. "
            "The existing target was left untouched; inspect it with the storage administration "
            "command or choose and configure a different absent path."
        ) from exc
    try:
        return _inspect_connection(connection)
    except sqlite3.Error as exc:
        raise MetricsSchemaMismatchError(
            f"SQLite metrics database at {str(path.resolve())!r} could not be inspected safely. "
            "The existing target was left untouched; inspect it with the storage administration "
            "command or choose and configure a different absent path."
        ) from exc
    finally:
        connection.close()


def _inspect_connection(connection: sqlite3.Connection) -> SchemaReport:
    table_names = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    tables = {str(row[0]) for row in table_names}
    provider_rows = (
        connection.execute(TABLE_INFO_PROVIDER_ATTEMPTS_SQL).fetchall()
        if "provider_attempts" in tables
        else []
    )
    version_schema_rows = (
        connection.execute(TABLE_INFO_SCHEMA_VERSIONS_SQL).fetchall()
        if SCHEMA_VERSIONS_TABLE in tables
        else []
    )
    version_columns = tuple(str(row[1]) for row in version_schema_rows)
    version_rows = (
        connection.execute(SELECT_SCHEMA_VERSIONS_SQL).fetchall()
        if {"component", "version"}.issubset(version_columns)
        else []
    )
    expected_index_names = {definition.name for definition in SQLITE_REQUIRED_METRICS_INDEXES}
    index_definitions: list[IndexDefinition] = []
    if "provider_attempts" in tables:
        for row in connection.execute("PRAGMA index_list('provider_attempts')").fetchall():
            name = str(row[1])
            if name not in expected_index_names:
                continue
            column_rows = connection.execute(f"PRAGMA index_info('{name}')").fetchall()
            index_definitions.append(
                IndexDefinition(
                    name=name,
                    table="provider_attempts",
                    unique=bool(row[2]),
                    columns=tuple(str(column[2]) for column in column_rows),
                )
            )
    return inspect_schema_rows(
        table_names=table_names,
        provider_schema_rows=provider_rows,
        version_schema_rows=version_schema_rows,
        version_rows=version_rows,
        index_definitions=index_definitions,
        required_indexes=SQLITE_REQUIRED_METRICS_INDEXES,
    )


def _build_score_aggregate_sql(query: ScoreAggregateQuery) -> tuple[str, tuple[object, ...]]:
    if not isinstance(query, ScoreAggregateQuery):
        raise TypeError("query must be a ScoreAggregateQuery")
    requested_values = ", ".join("(?, ?, ?, ?)" for _ in query.providers)
    params: list[object] = []
    for position, provider in enumerate(query.providers):
        params.extend((position, provider.provider_id, provider.model, provider.protocol.value))

    if isinstance(query.weighting, ExponentialScoreWeighting):
        weight_sql = (
            "CASE WHEN p.id IS NULL THEN 0.0 ELSE "
            "pow(0.5, ((julianday(?) - julianday(p.timestamp)) * 24.0) / ?) END"
        )
        params.extend((query.reference_time.isoformat(), query.weighting.half_life_hours))
    else:
        weight_sql = "CASE WHEN p.id IS NULL THEN 0.0 ELSE 1.0 END"

    scope_sql = ""
    join_params: list[object] = [
        query.since.isoformat(),
        query.call_type.value,
    ]
    if query.metrics_scope is not None:
        scope_sql = " AND p.metrics_scope = ?"
        join_params.append(query.metrics_scope)
    params.extend(join_params)
    params.extend((ErrorCategory.RATE_LIMIT.value, ErrorCategory.TIMEOUT.value))

    sql = f"""
WITH requested(position, provider_id, model, protocol) AS (
    VALUES {requested_values}
),
matched AS (
    SELECT
        requested.position,
        requested.provider_id AS requested_provider_id,
        p.id IS NOT NULL AS has_event,
        p.success,
        p.latency_ms,
        p.error_type,
        {weight_sql} AS event_weight
    FROM requested
    LEFT JOIN provider_attempts AS p
        ON p.provider_id = requested.provider_id
        AND p.model = requested.model
        AND p.protocol = requested.protocol
        AND p.timestamp >= ?
        AND p.call_type = ?{scope_sql}
)
SELECT
    requested_provider_id,
    COALESCE(SUM(CASE WHEN has_event THEN event_weight ELSE 0.0 END), 0.0),
    COALESCE(SUM(CASE WHEN has_event AND success = 1 THEN event_weight ELSE 0.0 END), 0.0),
    COALESCE(SUM(
        CASE
            WHEN has_event AND success = 1 AND latency_ms IS NOT NULL THEN event_weight
            ELSE 0.0
        END
    ), 0.0),
    COALESCE(SUM(
        CASE
            WHEN has_event AND success = 1 AND latency_ms IS NOT NULL
                THEN event_weight * latency_ms
            ELSE 0.0
        END
    ), 0.0),
    COALESCE(SUM(CASE WHEN has_event AND success = 0 THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(
        CASE WHEN has_event AND success = 0 AND error_type = ? THEN 1 ELSE 0 END
    ), 0),
    COALESCE(SUM(
        CASE WHEN has_event AND success = 0 AND error_type = ? THEN 1 ELSE 0 END
    ), 0)
FROM matched
GROUP BY position, requested_provider_id
ORDER BY position
"""
    return sql, tuple(params)


def _row_to_score_aggregate(row: Any) -> ScoreAggregate:
    if len(row) != 8:
        raise ValueError(f"score aggregate row has {len(row)} values; expected 8")
    return ScoreAggregate(
        provider_id=str(row[0]),
        attempt_weight=float(row[1]),
        success_weight=float(row[2]),
        successful_latency_weight=float(row[3]),
        successful_latency_total_ms=float(row[4]),
        recent_error_count=row[5],
        rate_limit_count=row[6],
        timeout_count=row[7],
    )
