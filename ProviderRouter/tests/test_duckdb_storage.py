from __future__ import annotations

import importlib.util
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_provider_router import ApiProtocol, CallType, MetricsEvent
from llm_provider_router.storage.base import MetricsSchemaMismatchError
from llm_provider_router.storage.duckdb import DuckDBMetricsStore

_DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None
requires_duckdb = pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed")


@requires_duckdb
def test_default_path_resolves_under_redirected_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    store = DuckDBMetricsStore()

    assert store.path == tmp_path / ".nygen_router" / "metrics.duckdb"
    assert not store.path.exists()  # not created until first connection

    store.record_attempt(
        MetricsEvent(
            provider_id="provider_a",
            metrics_scope="test",
            call_type=CallType.REGULAR,
            provider_name="provider_a",
            model="model-a",
            protocol=ApiProtocol.OPENAI_CHAT,
            success=True,
            latency_ms=1.0,
        )
    )

    assert store.path.exists()
    assert store.path.parent.exists()
    store.close()


@requires_duckdb
def test_custom_path_is_honored(tmp_path: Path) -> None:
    custom_path = tmp_path / "custom" / "my_metrics.duckdb"
    store = DuckDBMetricsStore(custom_path)

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


def test_sdk_available_false_logs_exactly_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The seam works with duckdb installed too -- sdk_available overrides the real check."""
    with caplog.at_level(logging.WARNING, logger="llm_provider_router.storage.duckdb"):
        DuckDBMetricsStore(sdk_available=False)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "[duckdb]" in warnings[0].message


def test_sdk_available_false_warns_only_once_when_router_writes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from llm_provider_router import CallVariant, ProviderConfig, ProviderRouter

    class _Adapter:
        def invoke(self, operation: str, arguments: dict[str, object]) -> str:
            return "response"

    config = ProviderConfig(
        provider_id="provider_a",
        name="provider_a",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://provider-a.example.com/v1",
        api_key="secret",
    )
    calls = [
        CallVariant(
            call_type=CallType.REGULAR,
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={"messages": []},
        )
    ]

    with caplog.at_level(logging.WARNING):
        store = DuckDBMetricsStore(sdk_available=False)
        router = ProviderRouter(
            metrics_scope="test",
            providers=[config],
            adapter_factory=lambda _: _Adapter(),
            metrics_store=store,
        )
        assert router.invoke(calls) == "response"
        assert router.invoke(calls) == "response"

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "[duckdb]" in warnings[0].message


@requires_duckdb
def test_incompatible_legacy_table_is_detected_and_left_unchanged(
    tmp_path: Path,
) -> None:
    """PR29 never alters, deletes, renames, or backfills a legacy table."""
    import duckdb

    path = tmp_path / "metrics.duckdb"
    recorded_at = datetime.now(UTC) - timedelta(minutes=5)
    connection = duckdb.connect(str(path))
    connection.execute(
        """
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
    )
    connection.execute(
        "INSERT INTO provider_attempts "
        "(id, timestamp, provider_name, model, protocol, success, latency_ms, error_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["old", recorded_at.isoformat(), "provider_a", "model-a", "openai_chat", 1, 5.0, None],
    )
    connection.close()

    store = DuckDBMetricsStore(path)
    with pytest.raises(MetricsSchemaMismatchError, match="left untouched"):
        store.query_recent(since=recorded_at - timedelta(seconds=1))

    inspection = duckdb.connect(str(path), read_only=True)
    try:
        columns = inspection.execute("PRAGMA table_info('provider_attempts')").fetchall()
        rows = inspection.execute("SELECT * FROM provider_attempts").fetchall()
        tables = inspection.execute("SHOW TABLES").fetchall()
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


@requires_duckdb
def test_query_recent_reads_back_recorded_events(tmp_path: Path) -> None:
    store = DuckDBMetricsStore(tmp_path / "metrics.duckdb")
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
            latency_ms=42.0,
        )
    )

    events = store.query_recent(since=since)

    assert len(events) == 1
    assert events[0].provider_name == "provider_a"
    store.close()
