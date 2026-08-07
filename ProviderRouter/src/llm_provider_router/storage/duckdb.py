from __future__ import annotations

import importlib.util
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    DUCKDB_REQUIRED_METRICS_INDEXES,
    SCHEMA_VERSIONS_TABLE,
    SELECT_SCHEMA_VERSIONS_SQL,
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

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger(__name__)


class DuckDBMetricsStore:
    """DuckDB-backed MetricsStore -- the embedded, no-server-to-run default.

    Single-process by design: DuckDB allows one writing process per file. Do
    not build cross-process coordination here -- users who need several local
    processes sharing one store should use SQLiteMetricsStore instead.
    Within one process the store is thread-safe: one lock serializes every
    database operation (connection creation, reads, writes, close) on the
    single shared connection, and is never held outside database work.
    Metrics v2 keeps raw query_recent for direct callers and executes one
    backend aggregate query for scoring. Measured DuckDB plans stayed
    sequential with or without candidate ART indexes, so v2 retains none.
    """

    def __init__(
        self, path: str | Path | None = None, *, sdk_available: bool | None = None
    ) -> None:
        self.path = (
            Path(path) if path is not None else Path.home() / ".nygen_router" / "metrics.duckdb"
        )
        available = (
            importlib.util.find_spec("duckdb") is not None
            if sdk_available is None
            else sdk_available
        )
        if not available:
            logger.warning(
                "duckdb is not installed: metrics persistence will not work. Run "
                'pip install "llm-provider-router[duckdb]", or pass a different metrics_store '
                "to ProviderRouter."
            )
        self._sdk_available = available
        self._connection: duckdb.DuckDBPyConnection | None = None
        # Serializes all use of the single connection, including its lazy
        # creation and close: one database operation at a time from any
        # thread. Held only for database work, never around callers' other
        # activity.
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """Whether the optional DuckDB dependency was present at construction."""
        return self._sdk_available

    def record_attempt(self, event: MetricsEvent) -> None:
        params = event_to_params(event)
        with self._lock:
            connection = self._connect()
            connection.execute(INSERT_PROVIDER_ATTEMPT_SQL, list(params))

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
            rows: list[Any] = connection.execute(query, params).fetchall()
        return [row_to_event(row) for row in rows]

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        """Return one validated intermediate-total row per requested provider."""
        sql, params = _build_score_aggregate_sql(query)
        with self._lock:
            rows: list[Any] = self._connect().execute(sql, list(params)).fetchall()
        aggregates = [_row_to_score_aggregate(row) for row in rows]
        validated = validate_score_aggregates(query, aggregates)
        return [validated[provider.provider_id] for provider in query.providers]

    def _explain_score_aggregates(self, query: ScoreAggregateQuery) -> tuple[str, ...]:
        """Return private, engine-neutral plan text for deterministic validation."""
        sql, params = _build_score_aggregate_sql(query)
        with self._lock:
            rows: list[Any] = self._connect().execute(f"EXPLAIN {sql}", list(params)).fetchall()
        return tuple(" | ".join(str(value) for value in row) for row in rows)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _connect(self) -> duckdb.DuckDBPyConnection:
        # Callers hold self._lock; never call this without it.
        if not self._sdk_available:
            raise ImportError(
                'duckdb is not installed; install it with pip install "llm-provider-router[duckdb]"'
            )
        if self._connection is None:
            import duckdb

            existed = self.path.exists()
            if existed:
                try:
                    inspection = duckdb.connect(str(self.path), read_only=True)
                    try:
                        report = _inspect_connection(inspection)
                    finally:
                        inspection.close()
                except Exception as exc:
                    if isinstance(exc, MetricsSchemaMismatchError):
                        raise
                    raise MetricsSchemaMismatchError(
                        f"DuckDB metrics database at {str(self.path.resolve())!r} could not be "
                        "inspected safely. The existing target was left untouched; inspect it "
                        "with the storage administration command or choose and configure a "
                        "different absent path."
                    ) from exc
                validate_runtime_schema(report, backend="DuckDB", path=str(self.path.resolve()))
            else:
                from llm_provider_router.storage.admin import LocalBackend, create_database

                create_database(LocalBackend.DUCKDB, self.path)
            connection = duckdb.connect(str(self.path))
            self._connection = connection
        return self._connection


def _inspect_connection(connection: duckdb.DuckDBPyConnection) -> SchemaReport:
    table_names: list[Any] = connection.execute("SHOW TABLES").fetchall()
    tables = {str(row[0]) for row in table_names}
    provider_rows: list[Any] = (
        connection.execute(TABLE_INFO_PROVIDER_ATTEMPTS_SQL).fetchall()
        if "provider_attempts" in tables
        else []
    )
    version_schema_rows: list[Any] = (
        connection.execute(TABLE_INFO_SCHEMA_VERSIONS_SQL).fetchall()
        if SCHEMA_VERSIONS_TABLE in tables
        else []
    )
    version_columns = tuple(str(row[1]) for row in version_schema_rows)
    version_rows: list[Any] = (
        connection.execute(SELECT_SCHEMA_VERSIONS_SQL).fetchall()
        if {"component", "version"}.issubset(version_columns)
        else []
    )
    index_definitions: list[IndexDefinition] = []
    expected_names = tuple(definition.name for definition in DUCKDB_REQUIRED_METRICS_INDEXES)
    if "provider_attempts" in tables and expected_names:
        placeholders = ", ".join("?" for _ in expected_names)
        index_rows: list[Any] = connection.execute(
            "SELECT index_name, table_name, is_unique, expressions "
            f"FROM duckdb_indexes() WHERE index_name IN ({placeholders}) ORDER BY index_name",
            list(expected_names),
        ).fetchall()
        index_definitions = [
            IndexDefinition(
                name=str(row[0]),
                table=str(row[1]),
                unique=bool(row[2]),
                columns=_duckdb_index_columns(row[3]),
            )
            for row in index_rows
        ]
    return inspect_schema_rows(
        table_names=table_names,
        provider_schema_rows=provider_rows,
        version_schema_rows=version_schema_rows,
        version_rows=version_rows,
        index_definitions=index_definitions,
        required_indexes=DUCKDB_REQUIRED_METRICS_INDEXES,
    )


def _duckdb_index_columns(expressions: object) -> tuple[str, ...]:
    """Normalize DuckDB's catalog expression list for project-owned simple indexes."""
    if isinstance(expressions, (tuple, list)):
        return tuple(_duckdb_index_column(value) for value in expressions)
    text = str(expressions).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return ()
    return tuple(_duckdb_index_column(value) for value in text.split(","))


def _duckdb_index_column(value: object) -> str:
    return str(value).strip().strip("'").strip('"')


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
            "POWER(0.5, (EPOCH(CAST(? AS TIMESTAMPTZ)) "
            "- EPOCH(CAST(p.timestamp AS TIMESTAMPTZ))) / 3600.0 / ?) END"
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
