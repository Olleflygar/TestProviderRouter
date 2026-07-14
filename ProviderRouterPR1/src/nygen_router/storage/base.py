from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from nygen_router.config import ApiProtocol
from nygen_router.metrics import MetricsEvent


class MetricsStore(Protocol):
    """The minimum interface every metrics backend implements.

    Deliberately just record + query: aggregation happens in Python over
    query_recent's output (PR 7), never in per-backend SQL, so a custom
    backend stays trivial to implement.
    """

    def record_attempt(self, event: MetricsEvent) -> None: ...

    def query_recent(
        self,
        *,
        since: datetime,
        provider_name: str | None = None,
        model: str | None = None,
    ) -> list[MetricsEvent]: ...


# Shared by DuckDBMetricsStore and SQLiteMetricsStore so the two engines stay
# byte-identical in schema and behavior -- only columns with a real data
# source today; later PRs add theirs (tokens: PR 24, request_size_bucket:
# PR 11, required_tools: PR 21, cost: PR 6, stream/total_duration_ms: PR 23).
CREATE_PROVIDER_ATTEMPTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS provider_attempts (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model TEXT NOT NULL,
    protocol TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL,
    error_type TEXT
)
"""

_COLUMNS = "id, timestamp, provider_name, model, protocol, success, latency_ms, error_type"

INSERT_PROVIDER_ATTEMPT_SQL = (
    f"INSERT INTO provider_attempts ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

_SELECT_PROVIDER_ATTEMPTS_SQL = f"SELECT {_COLUMNS} FROM provider_attempts WHERE timestamp >= ?"


def event_to_params(event: MetricsEvent) -> tuple[object, ...]:
    """Serialize a MetricsEvent to positional params for INSERT_PROVIDER_ATTEMPT_SQL.

    The timestamp is serialized to ISO-8601 UTC text in Python -- never via SQL
    date functions -- so TEXT comparison stays chronologically correct and
    both engines behave identically.
    """
    return (
        event.id,
        event.timestamp.isoformat(),
        event.provider_name,
        event.model,
        event.protocol.value,
        event.success,
        event.latency_ms,
        event.error_type,
    )


def build_query_recent_sql(
    *, since: datetime, provider_name: str | None, model: str | None
) -> tuple[str, list[object]]:
    """Build the query_recent SQL/params, validating `since` is timezone-aware."""
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    query = _SELECT_PROVIDER_ATTEMPTS_SQL
    params: list[object] = [since.isoformat()]
    if provider_name is not None:
        query += " AND provider_name = ?"
        params.append(provider_name)
    if model is not None:
        query += " AND model = ?"
        params.append(model)
    query += " ORDER BY timestamp ASC"
    return query, params


def row_to_event(row: Sequence[Any]) -> MetricsEvent:
    """Parse one query_recent row back into a MetricsEvent.

    Mirrors event_to_params's column order. Timestamps are parsed back with
    datetime.fromisoformat(); protocol is parsed back with ApiProtocol(value).
    """
    id_, timestamp, provider_name, model, protocol, success, latency_ms, error_type = row
    return MetricsEvent(
        id=str(id_),
        timestamp=datetime.fromisoformat(str(timestamp)),
        provider_name=str(provider_name),
        model=str(model),
        protocol=ApiProtocol(protocol),
        success=bool(success),
        latency_ms=None if latency_ms is None else float(latency_ms),
        error_type=None if error_type is None else str(error_type),
    )
