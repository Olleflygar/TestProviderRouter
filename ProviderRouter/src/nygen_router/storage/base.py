from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from nygen_router.config import ApiProtocol
from nygen_router.metrics import MetricsEvent
from nygen_router.types import CallType


class MetricsStore(Protocol):
    """The minimum interface every metrics backend implements."""

    def record_attempt(self, event: MetricsEvent) -> None: ...

    def query_recent(
        self,
        *,
        since: datetime,
        metrics_scope: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        protocol: ApiProtocol | None = None,
        call_type: CallType | None = None,
    ) -> list[MetricsEvent]: ...


class MetricsSchemaMismatchError(RuntimeError):
    """An existing provider_attempts table is not the exact supported schema."""


CREATE_PROVIDER_ATTEMPTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS provider_attempts (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    metrics_scope TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model TEXT NOT NULL,
    protocol TEXT NOT NULL,
    call_type TEXT NOT NULL,
    success INTEGER NOT NULL,
    stream_opened INTEGER,
    latency_ms REAL,
    total_duration_ms REAL,
    error_type TEXT,
    request_size_bucket TEXT
)
"""

COLUMN_NAMES = (
    "id",
    "timestamp",
    "metrics_scope",
    "provider_id",
    "provider_name",
    "model",
    "protocol",
    "call_type",
    "success",
    "stream_opened",
    "latency_ms",
    "total_duration_ms",
    "error_type",
    "request_size_bucket",
)

_COLUMNS_SQL = ", ".join(COLUMN_NAMES)
INSERT_PROVIDER_ATTEMPT_SQL = (
    f"INSERT INTO provider_attempts ({_COLUMNS_SQL}) VALUES "
    f"({', '.join('?' for _ in COLUMN_NAMES)})"
)
TABLE_INFO_SQL = "PRAGMA table_info('provider_attempts')"
_SELECT_PROVIDER_ATTEMPTS_SQL = f"SELECT {_COLUMNS_SQL} FROM provider_attempts WHERE timestamp >= ?"

# Logical schema shared by SQLite and DuckDB. DuckDB reports TEXT as VARCHAR
# and REAL as FLOAT, while SQLite reports the spellings used in the DDL.
_EXPECTED_SCHEMA = (
    ("id", "text", False, True),
    ("timestamp", "text", True, False),
    ("metrics_scope", "text", True, False),
    ("provider_id", "text", True, False),
    ("provider_name", "text", True, False),
    ("model", "text", True, False),
    ("protocol", "text", True, False),
    ("call_type", "text", True, False),
    ("success", "integer", True, False),
    ("stream_opened", "integer", False, False),
    ("latency_ms", "real", False, False),
    ("total_duration_ms", "real", False, False),
    ("error_type", "text", False, False),
    ("request_size_bucket", "text", False, False),
)


def validate_provider_attempts_schema(rows: Sequence[Sequence[Any]]) -> None:
    """Reject any existing table that is not exactly the PR29 schema."""
    actual = []
    for row in rows:
        name = str(row[1])
        logical_type = _logical_type(str(row[2]))
        not_null = bool(row[3])
        default = row[4]
        primary_key = bool(row[5])
        # SQLite reports a TEXT PRIMARY KEY as nullable even though the primary
        # key itself prevents NULL identity values. Compare that column by PK.
        if primary_key:
            not_null = False
        actual.append((name, logical_type, not_null, primary_key, default))
    expected = [(*column, None) for column in _EXPECTED_SCHEMA]
    if actual != expected:
        actual_names = ", ".join(item[0] for item in actual) or "<none>"
        raise MetricsSchemaMismatchError(
            "Existing provider_attempts schema is incompatible with this nygen-router "
            f"version (found columns: {actual_names}). The database was left untouched; "
            "move or replace it manually if its legacy history is no longer needed."
        )


def _logical_type(value: str) -> str:
    normalized = value.upper()
    if normalized in {"TEXT", "VARCHAR"}:
        return "text"
    if normalized in {"INTEGER", "INT"}:
        return "integer"
    if normalized in {"REAL", "FLOAT", "DOUBLE"}:
        return "real"
    return normalized.lower()


def event_to_params(event: MetricsEvent) -> tuple[object, ...]:
    """Serialize an event in the single shared column order.

    Timestamps are compared lexically as ISO text, so every timezone-aware
    value must be stored in the same UTC offset representation.
    """
    timestamp = event.timestamp
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(UTC)
    return (
        event.id,
        timestamp.isoformat(),
        event.metrics_scope,
        event.provider_id,
        event.provider_name,
        event.model,
        event.protocol.value,
        event.call_type.value,
        event.success,
        event.stream_opened,
        event.latency_ms,
        event.total_duration_ms,
        event.error_type,
        event.request_size_bucket,
    )


def build_query_recent_sql(
    *,
    since: datetime,
    metrics_scope: str | None,
    provider_id: str | None,
    model: str | None,
    protocol: ApiProtocol | None,
    call_type: CallType | None,
) -> tuple[str, list[object]]:
    """Build a parameterized recent-history query for every identity dimension."""
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    query = _SELECT_PROVIDER_ATTEMPTS_SQL
    # Normalized to UTC because the comparison against stored rows is lexical:
    # a non-UTC offset would compare wrongly against the stored +00:00 text.
    params: list[object] = [since.astimezone(UTC).isoformat()]
    filters: tuple[tuple[str, object | None], ...] = (
        ("metrics_scope", metrics_scope),
        ("provider_id", provider_id),
        ("model", model),
        ("protocol", None if protocol is None else protocol.value),
        ("call_type", None if call_type is None else call_type.value),
    )
    for column, value in filters:
        if value is not None:
            query += f" AND {column} = ?"
            params.append(value)
    query += " ORDER BY timestamp ASC"
    return query, params


def row_to_event(row: Sequence[Any]) -> MetricsEvent:
    """Deserialize an event from the single shared column order."""
    (
        id_,
        timestamp,
        metrics_scope,
        provider_id,
        provider_name,
        model,
        protocol,
        call_type,
        success,
        stream_opened,
        latency_ms,
        total_duration_ms,
        error_type,
        request_size_bucket,
    ) = row
    return MetricsEvent(
        id=str(id_),
        timestamp=datetime.fromisoformat(str(timestamp)),
        metrics_scope=str(metrics_scope),
        provider_id=str(provider_id),
        provider_name=str(provider_name),
        model=str(model),
        protocol=ApiProtocol(protocol),
        call_type=CallType(call_type),
        success=bool(success),
        stream_opened=None if stream_opened is None else bool(stream_opened),
        latency_ms=None if latency_ms is None else float(latency_ms),
        total_duration_ms=None if total_duration_ms is None else float(total_duration_ms),
        error_type=None if error_type is None else str(error_type),
        request_size_bucket=(None if request_size_bucket is None else str(request_size_bucket)),
    )
