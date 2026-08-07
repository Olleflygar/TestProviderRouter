from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from llm_provider_router.config import ApiProtocol
from llm_provider_router.metrics import MetricsEvent
from llm_provider_router.storage.schema import COLUMN_NAMES
from llm_provider_router.storage.schema import MetricsSchemaMismatchError as MetricsSchemaMismatchError
from llm_provider_router.storage.score_aggregation import ScoreAggregate, ScoreAggregateQuery
from llm_provider_router.types import CallType


class MetricsStore(Protocol):
    """Mandatory event, raw-history, and bounded score-aggregate backend contract.

    ``query_recent`` remains the direct raw-event API. ScoreBasedPolicy always
    uses ``query_score_aggregates`` and has no legacy two-method fallback.
    """

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

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]: ...


_COLUMNS_SQL = ", ".join(COLUMN_NAMES)
INSERT_PROVIDER_ATTEMPT_SQL = (
    f"INSERT INTO provider_attempts ({_COLUMNS_SQL}) VALUES "
    f"({', '.join('?' for _ in COLUMN_NAMES)})"
)
_SELECT_PROVIDER_ATTEMPTS_SQL = f"SELECT {_COLUMNS_SQL} FROM provider_attempts WHERE timestamp >= ?"


def event_to_record(event: MetricsEvent) -> dict[str, object]:
    """Convert one domain event to the shared named logical storage record."""
    timestamp = event.timestamp
    if timestamp.tzinfo is None:
        raise ValueError("event timestamp must be timezone-aware")
    return {
        "id": event.id,
        "timestamp": timestamp.astimezone(UTC),
        "metrics_scope": event.metrics_scope,
        "provider_id": event.provider_id,
        "provider_name": event.provider_name,
        "model": event.model,
        "protocol": event.protocol.value,
        "call_type": event.call_type.value,
        "success": event.success,
        "stream_opened": event.stream_opened,
        "latency_ms": event.latency_ms,
        "total_duration_ms": event.total_duration_ms,
        "error_type": event.error_type,
    }


def record_to_event(record: Mapping[str, object]) -> MetricsEvent:
    """Convert a named local-text or future native-datetime record to an event."""
    raw_timestamp = record["timestamp"]
    if isinstance(raw_timestamp, datetime):
        timestamp = raw_timestamp
    else:
        timestamp = datetime.fromisoformat(str(raw_timestamp))
    if timestamp.tzinfo is None:
        raise ValueError("stored event timestamp must be timezone-aware")
    return MetricsEvent(
        id=str(record["id"]),
        timestamp=timestamp.astimezone(UTC),
        metrics_scope=str(record["metrics_scope"]),
        provider_id=str(record["provider_id"]),
        provider_name=str(record["provider_name"]),
        model=str(record["model"]),
        protocol=ApiProtocol(str(record["protocol"])),
        call_type=CallType(str(record["call_type"])),
        success=_database_bool(record["success"], column="success"),
        stream_opened=_optional_database_bool(record["stream_opened"], column="stream_opened"),
        latency_ms=_optional_float(record["latency_ms"]),
        total_duration_ms=_optional_float(record["total_duration_ms"]),
        error_type=None if record["error_type"] is None else str(record["error_type"]),
    )


def event_to_params(event: MetricsEvent) -> tuple[object, ...]:
    """Derive local ISO-text positional parameters from the named record."""
    record = event_to_record(event)
    timestamp = record["timestamp"]
    assert isinstance(timestamp, datetime)
    local_record = {**record, "timestamp": timestamp.isoformat()}
    return tuple(local_record[column] for column in COLUMN_NAMES)


def build_query_recent_sql(
    *,
    since: datetime,
    metrics_scope: str | None,
    provider_id: str | None,
    model: str | None,
    protocol: ApiProtocol | None,
    call_type: CallType | None,
) -> tuple[str, list[object]]:
    """Build the raw chronological query retained for direct callers and diagnosis."""
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
    if len(row) != len(COLUMN_NAMES):
        raise ValueError(f"stored event row has {len(row)} values; expected {len(COLUMN_NAMES)}")
    return record_to_event(dict(zip(COLUMN_NAMES, row, strict=True)))


def _database_bool(value: object, *, column: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError(f"stored {column} must be a boolean or integer 0/1")


def _optional_database_bool(value: object, *, column: str) -> bool | None:
    return None if value is None else _database_bool(value, column=column)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]
