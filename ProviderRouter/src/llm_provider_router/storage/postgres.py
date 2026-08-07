from __future__ import annotations

import importlib.util
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from llm_provider_router.config import ApiProtocol
from llm_provider_router.errors import ConfigError, ErrorCategory
from llm_provider_router.metrics import MetricsEvent
from llm_provider_router.storage.base import (
    build_query_recent_sql,
    event_to_record,
    record_to_event,
)
from llm_provider_router.storage.schema import (
    COLUMN_NAMES,
    SCHEMA_VERSIONS_TABLE,
    SELECT_POSTGRES_COLUMNS_SQL,
    SELECT_POSTGRES_INDEXES_SQL,
    SELECT_POSTGRES_TABLES_SQL,
    SELECT_SCHEMA_VERSIONS_SQL,
    IndexDefinition,
    MetricsSchemaMismatchError,
    SchemaReport,
    inspect_postgres_schema_rows,
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
    from psycopg import Connection
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

# 13 columns per row against PostgreSQL's 65535-parameter ceiling. Chunking
# keeps one caller-visible batch all-or-nothing inside a single transaction
# while never building a statement the server would refuse.
_MAX_ROWS_PER_INSERT = 1000

_COLUMNS_SQL = ", ".join(COLUMN_NAMES)
_ROW_PLACEHOLDER = f"({', '.join('%s' for _ in COLUMN_NAMES)})"


class PostgresPoolMode(StrEnum):
    """How this store reaches PostgreSQL, which decides pooling behavior.

    The mode is never inferred: a wrong guess fails intermittently rather than
    visibly, which is the exact failure this setting exists to prevent.
    """

    DIRECT = "direct"
    SESSION_POOLER = "session_pooler"
    TRANSACTION_POOLER = "transaction_pooler"


class PostgresConfig(BaseModel):
    """Validated connection behavior for PostgresMetricsStore.

    Timeouts default latency-first: bookkeeping never delays a provider
    response for long, at the cost of dropping rows on a poor link. Raise them
    (connect 10 / statement 5 / checkout 5 is the documented alternative) when
    complete history matters more than call latency.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pool_mode: PostgresPoolMode = PostgresPoolMode.DIRECT
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    statement_timeout_seconds: float = Field(default=2.0, gt=0)
    checkout_timeout_seconds: float = Field(default=2.0, gt=0)
    min_pool_size: int = Field(default=1, ge=0)
    max_pool_size: int = Field(default=4, ge=1)
    # None means "whatever the URL asks for, or require when it is silent", so
    # a caller who spells out a stronger mode in their connection string is
    # never quietly downgraded by this default.
    sslmode: str | None = None
    sslrootcert: str | None = None
    allow_unencrypted: bool = False

    def validated(self) -> PostgresConfig:
        if self.max_pool_size < self.min_pool_size:
            raise ConfigError("max_pool_size must not be smaller than min_pool_size")
        return self


# libpq modes that permit an unencrypted connection, including its own default.
UNENCRYPTED_SSL_MODES = frozenset({"disable", "allow", "prefer"})
DEFAULT_SSL_MODE = "require"


def resolve_sslmode(url: str, configured: str | None) -> str:
    """Explicit configuration wins, then the URL, then encrypted-by-default."""
    if configured is not None:
        return configured
    query = parse_qs(urlsplit(url).query)
    values = query.get("sslmode")
    if values and values[0]:
        return values[0]
    return DEFAULT_SSL_MODE


def _driver_is_installed() -> bool:
    """Whether psycopg can be found, treating any lookup failure as absent.

    Constructing a store must never raise merely because the optional driver
    is missing or its import machinery is restricted; that is reported as
    unavailability and surfaces at first use with an install hint.
    """
    try:
        return importlib.util.find_spec("psycopg") is not None
    except (ImportError, ValueError):
        return False


def redact_postgres_url(url: str) -> str:
    """Return the target with any password replaced, safe for errors and logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable PostgreSQL URL>"
    if parts.hostname is None:
        return "<PostgreSQL URL>"
    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += ":***"
        userinfo += "@"
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"{userinfo}{host}", parts.path, "", ""))


class PostgresMetricsStore:
    """PostgreSQL-backed MetricsStore for shared organizational history.

    Optional: install with pip install "llm-provider-router[postgres]". Works against
    any conventional PostgreSQL deployment and against Supabase through the
    standard PostgreSQL protocol -- never the Supabase Data API or client SDK.

    Unlike the two local stores this one holds no lock of its own. Its pool is
    built for concurrent use from many threads, and the router already
    serializes its own storage calls, so a store lock would guard nothing the
    router can do concurrently while giving one slow direct read a way to
    block all routing. Because it takes no lock, the router-before-store
    ordering rule cannot be violated here.

    The store never creates, alters, or migrates the remote schema. Provision
    it deliberately through llm-provider-router storage create --backend postgres, or
    by applying the published DDL with your own tooling.
    """

    def __init__(
        self,
        url: str,
        *,
        config: PostgresConfig | Mapping[str, object] | None = None,
        driver_available: bool | None = None,
    ) -> None:
        if not isinstance(url, str) or not url.strip():
            raise ConfigError("url must be a non-blank PostgreSQL connection string")
        self._url = url.strip()
        if config is None:
            resolved = PostgresConfig()
        elif isinstance(config, PostgresConfig):
            resolved = config
        else:
            resolved = PostgresConfig.model_validate(config)
        self.config = resolved.validated()
        self.effective_sslmode = resolve_sslmode(self._url, self.config.sslmode)
        if self.effective_sslmode in UNENCRYPTED_SSL_MODES and not self.config.allow_unencrypted:
            raise ConfigError(
                f"sslmode={self.effective_sslmode!r} permits sending credentials and routing "
                "history unencrypted. Pass allow_unencrypted=True to confirm this is "
                "deliberate, or use sslmode='require' (encrypted) or 'verify-full' "
                "(encrypted and authenticated)."
            )
        self.target = redact_postgres_url(self._url)

        available = _driver_is_installed() if driver_available is None else driver_available
        if not available:
            logger.warning(
                "psycopg is not installed: PostgreSQL metrics persistence will not work. Run "
                'pip install "llm-provider-router[postgres]", or pass a different metrics_store '
                "to ProviderRouter."
            )
        self._driver_available = available
        self._pool_instance: ConnectionPool[Connection[Any]] | None = None
        self._schema_validated = False

    @property
    def available(self) -> bool:
        """Whether the optional psycopg dependency was present at construction."""
        return self._driver_available

    def record_attempt(self, event: MetricsEvent) -> None:
        """Persist one attempt through the same batch path every write uses."""
        self.record_attempts((event,))

    def record_attempts(self, events: Sequence[MetricsEvent]) -> None:
        """Persist many attempts in one transaction: all rows land, or none do.

        Beyond the MetricsStore contract, and deliberately unused by the router
        today -- it still records one attempt at a time, before returning the
        provider's response. PR32 owns buffering; nothing here delays, queues,
        reorders, or drops a write.
        """
        rows = [
            tuple(event_to_record(event)[column] for column in COLUMN_NAMES) for event in events
        ]
        if not rows:
            return
        with self._connection() as connection:
            for start in range(0, len(rows), _MAX_ROWS_PER_INSERT):
                chunk = rows[start : start + _MAX_ROWS_PER_INSERT]
                sql = f"INSERT INTO provider_attempts ({_COLUMNS_SQL}) VALUES " + ", ".join(
                    _ROW_PLACEHOLDER for _ in chunk
                )
                connection.execute(sql, [value for row in chunk for value in row])

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
        # The shared builder emits the local backends' positional marker. Only
        # the marker differs; reusing the builder keeps the filter set, the
        # lower bound, and the ordering identical across all three backends.
        with self._connection() as connection:
            rows = connection.execute(query.replace("?", "%s"), params).fetchall()
        return [record_to_event(dict(zip(COLUMN_NAMES, row, strict=True))) for row in rows]

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        """Return one validated intermediate-total row per requested provider."""
        sql, params = _build_score_aggregate_sql(query)
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        aggregates = [_row_to_score_aggregate(row) for row in rows]
        validated = validate_score_aggregates(query, aggregates)
        return [validated[provider.provider_id] for provider in query.providers]

    def _explain_score_aggregates(self, query: ScoreAggregateQuery) -> tuple[str, ...]:
        """Return private, engine-neutral plan text for deterministic validation."""
        sql, params = _build_score_aggregate_sql(query)
        with self._connection() as connection:
            rows = connection.execute(f"EXPLAIN {sql}", params).fetchall()
        return tuple(" | ".join(str(value) for value in row) for row in rows)

    def close(self) -> None:
        """Release pooled connections. Idempotent; a later call reconnects."""
        pool = self._pool_instance
        if pool is not None:
            self._pool_instance = None
            self._schema_validated = False
            pool.close()

    def __enter__(self) -> PostgresMetricsStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def inspect_schema(self) -> SchemaReport:
        """Read the remote catalog without writing anything."""
        with self._connection(validate=False) as connection:
            return _inspect_connection(connection)

    def _connection(self, *, validate: bool = True) -> Any:
        pool = self._pool()
        if validate and not self._schema_validated:
            self._validate_schema(pool)
        return pool.connection()

    def _validate_schema(self, pool: ConnectionPool[Connection[Any]]) -> None:
        with pool.connection() as connection:
            report = _inspect_connection(connection)
        validate_runtime_schema(report, backend="PostgreSQL", path=self.target)
        self._schema_validated = True

    def _pool(self) -> ConnectionPool[Connection[Any]]:
        if not self._driver_available:
            raise ImportError(
                "psycopg is not installed; install it with "
                'pip install "llm-provider-router[postgres]"'
            )
        if self._pool_instance is None:
            from psycopg_pool import ConnectionPool

            config = self.config
            kwargs: dict[str, Any] = {
                "connect_timeout": max(1, int(round(config.connect_timeout_seconds))),
                "sslmode": self.effective_sslmode,
            }
            if config.sslrootcert is not None:
                kwargs["sslrootcert"] = config.sslrootcert
            self._pool_instance = ConnectionPool(
                self._url,
                min_size=config.min_pool_size,
                max_size=config.max_pool_size,
                timeout=config.checkout_timeout_seconds,
                kwargs=kwargs,
                configure=self._configure_connection,
                open=True,
            )
        return self._pool_instance

    def _configure_connection(self, connection: Connection[Any]) -> None:
        """Apply per-connection settings once, so no operation pays for them.

        The statement timeout is applied with a session SET rather than the
        connection's startup options: a managed pooler accepts the startup
        parameter and silently ignores it, which would leave every query
        unbounded while appearing configured.
        """
        milliseconds = max(1, int(round(self.config.statement_timeout_seconds * 1000)))
        connection.execute(f"SET statement_timeout = {milliseconds}")
        connection.commit()
        if self.config.pool_mode is PostgresPoolMode.TRANSACTION_POOLER:
            # Classic transaction poolers hand a server connection to another
            # client between transactions, which breaks server-side prepared
            # statements. Supabase's pooler was measured to tolerate them, but
            # the conservative default keeps self-hosted PgBouncer working.
            connection.prepare_threshold = None


def is_prepared_statement_pooler_error(exc: BaseException) -> bool:
    """Whether an error looks like prepared statements against a transaction pooler."""
    text = str(exc).lower()
    return "prepared statement" in text and ("does not exist" in text or "already exists" in text)


def explain_pooler_error(exc: BaseException) -> str:
    """Actionable guidance for the failure a wrong pooling mode produces."""
    return (
        f"{exc} -- this usually means the connection goes through a transaction pooler while "
        "server-side prepared statements are enabled. Construct PostgresMetricsStore with "
        "config={'pool_mode': 'transaction_pooler'} so prepared statements are disabled, or "
        "connect to the direct/session-pooler endpoint instead."
    )


def _inspect_connection(connection: Connection[Any]) -> SchemaReport:
    table_names = connection.execute(SELECT_POSTGRES_TABLES_SQL).fetchall()
    tables = {str(row[0]) for row in table_names}
    provider_rows = (
        connection.execute(SELECT_POSTGRES_COLUMNS_SQL, ("provider_attempts",)).fetchall()
        if "provider_attempts" in tables
        else []
    )
    version_schema_rows = (
        connection.execute(SELECT_POSTGRES_COLUMNS_SQL, (SCHEMA_VERSIONS_TABLE,)).fetchall()
        if SCHEMA_VERSIONS_TABLE in tables
        else []
    )
    version_columns = tuple(str(row[1]) for row in version_schema_rows)
    version_rows = (
        connection.execute(SELECT_SCHEMA_VERSIONS_SQL).fetchall()
        if {"component", "version"}.issubset(version_columns)
        else []
    )
    index_definitions: list[IndexDefinition] = []
    if "provider_attempts" in tables:
        columns_by_index: dict[str, tuple[bool, list[str]]] = {}
        for row in connection.execute(SELECT_POSTGRES_INDEXES_SQL).fetchall():
            name, unique, column = str(row[0]), bool(row[1]), str(row[2])
            entry = columns_by_index.setdefault(name, (unique, []))
            entry[1].append(column)
        index_definitions = [
            IndexDefinition(
                name=name, table="provider_attempts", unique=unique, columns=tuple(columns)
            )
            for name, (unique, columns) in columns_by_index.items()
        ]
    return inspect_postgres_schema_rows(
        table_names=table_names,
        provider_schema_rows=provider_rows,
        version_schema_rows=version_schema_rows,
        version_rows=version_rows,
        index_definitions=index_definitions,
    )


def _build_score_aggregate_sql(query: ScoreAggregateQuery) -> tuple[str, tuple[object, ...]]:
    if not isinstance(query, ScoreAggregateQuery):
        raise TypeError("query must be a ScoreAggregateQuery")
    requested_values = ", ".join("(%s::int, %s::text, %s::text, %s::text)" for _ in query.providers)
    params: list[object] = []
    for position, provider in enumerate(query.providers):
        params.extend((position, provider.provider_id, provider.model, provider.protocol.value))

    if isinstance(query.weighting, ExponentialScoreWeighting):
        weight_sql = (
            "CASE WHEN p.id IS NULL THEN 0.0 ELSE "
            "power(0.5, (EXTRACT(EPOCH FROM (%s::timestamptz - p.timestamp)) / 3600.0) / %s) END"
        )
        params.extend((query.reference_time, query.weighting.half_life_hours))
    else:
        weight_sql = "CASE WHEN p.id IS NULL THEN 0.0 ELSE 1.0 END"

    scope_sql = ""
    join_params: list[object] = [query.since, query.call_type.value]
    if query.metrics_scope is not None:
        scope_sql = " AND p.metrics_scope = %s"
        join_params.append(query.metrics_scope)
    params.extend(join_params)
    params.extend((ErrorCategory.RATE_LIMIT.value, ErrorCategory.TIMEOUT.value))

    sql = f"""
WITH requested(req_position, provider_id, model, protocol) AS (
    VALUES {requested_values}
),
matched AS (
    SELECT
        requested.req_position,
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
        AND p.timestamp >= %s
        AND p.call_type = %s{scope_sql}
)
SELECT
    requested_provider_id,
    COALESCE(SUM(CASE WHEN has_event THEN event_weight ELSE 0.0 END), 0.0),
    COALESCE(SUM(CASE WHEN has_event AND success THEN event_weight ELSE 0.0 END), 0.0),
    COALESCE(SUM(
        CASE
            WHEN has_event AND success AND latency_ms IS NOT NULL THEN event_weight
            ELSE 0.0
        END
    ), 0.0),
    COALESCE(SUM(
        CASE
            WHEN has_event AND success AND latency_ms IS NOT NULL
                THEN event_weight * latency_ms
            ELSE 0.0
        END
    ), 0.0),
    COALESCE(SUM(CASE WHEN has_event AND NOT success THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(
        CASE WHEN has_event AND NOT success AND error_type = %s THEN 1 ELSE 0 END
    ), 0),
    COALESCE(SUM(
        CASE WHEN has_event AND NOT success AND error_type = %s THEN 1 ELSE 0 END
    ), 0)
FROM matched
GROUP BY req_position, requested_provider_id
ORDER BY req_position
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
        recent_error_count=int(row[5]),
        rate_limit_count=int(row[6]),
        timeout_count=int(row[7]),
    )


__all__ = [
    "MetricsSchemaMismatchError",
    "PostgresConfig",
    "PostgresMetricsStore",
    "PostgresPoolMode",
    "redact_postgres_url",
]
