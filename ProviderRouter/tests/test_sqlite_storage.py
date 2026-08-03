from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nygen_router import ApiProtocol, CallType, MetricsEvent, SQLiteMetricsStore
from nygen_router.storage.base import MetricsSchemaMismatchError

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
            provider_id="provider_a",
            metrics_scope="test",
            call_type=CallType.REGULAR,
            provider_name="provider_a",
            model="model-a",
            protocol=ApiProtocol.OPENAI_CHAT,
            success=True,
        )
    )

    assert custom_path.exists()
    store.close()


def test_incompatible_legacy_table_is_detected_and_left_unchanged(
    tmp_path: Path,
) -> None:
    """PR29 never alters, deletes, renames, or backfills a legacy table."""
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
    with pytest.raises(MetricsSchemaMismatchError, match="left untouched"):
        store.record_attempt(
            MetricsEvent(
                metrics_scope="test",
                provider_id="provider_b",
                provider_name="provider_b",
                model="model-a",
                protocol=ApiProtocol.OPENAI_CHAT,
                call_type=CallType.REGULAR,
                success=True,
            )
        )

    inspection = sqlite3.connect(str(path))
    try:
        columns = inspection.execute("PRAGMA table_info('provider_attempts')").fetchall()
        rows = inspection.execute("SELECT * FROM provider_attempts").fetchall()
        tables = inspection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    finally:
        inspection.close()
    assert [row[1] for row in columns] == [
        "id",
        "timestamp",
        "provider_name",
        "model",
        "protocol",
        "success",
        "latency_ms",
        "error_type",
    ]
    assert rows == [
        ("old", recorded_at.isoformat(), "provider_a", "model-a", "openai_chat", 1, 5.0, None)
    ]
    assert tables == [("provider_attempts",)]


def test_works_with_stdlib_only_no_optional_dependencies(tmp_path: Path) -> None:
    """SQLiteMetricsStore uses only Python's stdlib sqlite3, no extra install."""
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite")
    since = datetime.now(UTC) - timedelta(minutes=1)

    store.record_attempt(
        MetricsEvent(
            provider_id="provider_a",
            metrics_scope="test",
            call_type=CallType.REGULAR,
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
