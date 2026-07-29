from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nygen_router import ApiProtocol, MetricsEvent, SQLiteMetricsStore

# The provider_attempts schema as it stood before PR 23 added its two columns,
# so a file written by an earlier version can be built here for real.
_PRE_PR23_SCHEMA_SQL = """
CREATE TABLE provider_attempts (
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

_PRE_PR23_INSERT_SQL = (
    "INSERT INTO provider_attempts "
    "(id, timestamp, provider_name, model, protocol, success, latency_ms, error_type) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def test_path_is_required_with_no_default() -> None:
    parameters = inspect.signature(SQLiteMetricsStore.__init__).parameters
    assert parameters["path"].default is inspect.Parameter.empty


def test_custom_path_is_honored(tmp_path: Path) -> None:
    custom_path = tmp_path / "custom" / "my_metrics.sqlite"
    store = SQLiteMetricsStore(custom_path)

    assert store.path == custom_path

    store.record_attempt(
        MetricsEvent(
            provider_name="provider_a",
            model="model-a",
            protocol=ApiProtocol.OPENAI_CHAT,
            success=True,
        )
    )

    assert custom_path.exists()
    store.close()


def test_metrics_file_written_before_the_stream_columns_is_migrated_on_connect(
    tmp_path: Path,
) -> None:
    """CREATE TABLE IF NOT EXISTS leaves an old file alone, so the columns are added instead."""
    path = tmp_path / "metrics.sqlite"
    recorded_at = datetime.now(UTC) - timedelta(minutes=5)
    connection = sqlite3.connect(str(path))
    connection.execute(_PRE_PR23_SCHEMA_SQL)
    connection.execute(
        _PRE_PR23_INSERT_SQL,
        ("old", recorded_at.isoformat(), "provider_a", "model-a", "openai_chat", 1, 5.0, None),
    )
    connection.commit()
    connection.close()

    store = SQLiteMetricsStore(path)
    store.record_attempt(
        MetricsEvent(
            provider_name="provider_b",
            model="model-a",
            protocol=ApiProtocol.OPENAI_CHAT,
            success=True,
            latency_ms=8.0,
            stream=True,
            total_duration_ms=900.0,
        )
    )
    events = store.query_recent(since=recorded_at - timedelta(seconds=1))

    old_event, new_event = events
    assert old_event.id == "old"  # the pre-existing row survives the migration
    assert old_event.stream is False  # backfilled as the non-stream it was
    assert old_event.total_duration_ms is None
    assert new_event.stream is True
    assert new_event.total_duration_ms == 900.0
    store.close()


def test_works_with_stdlib_only_no_optional_dependencies(tmp_path: Path) -> None:
    """SQLiteMetricsStore uses only Python's stdlib sqlite3, no extra install."""
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite")
    since = datetime.now(UTC) - timedelta(minutes=1)

    store.record_attempt(
        MetricsEvent(
            provider_name="provider_a",
            model="model-a",
            protocol=ApiProtocol.OPENAI_CHAT,
            success=True,
            latency_ms=7.5,
        )
    )
    events = store.query_recent(since=since)

    assert len(events) == 1
    assert events[0].provider_name == "provider_a"
    store.close()
