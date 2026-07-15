from __future__ import annotations

import importlib.util
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nygen_router import ApiProtocol, MetricsEvent
from nygen_router.storage.duckdb import DuckDBMetricsStore

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
    with caplog.at_level(logging.WARNING, logger="nygen_router.storage.duckdb"):
        DuckDBMetricsStore(sdk_available=False)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "[duckdb]" in warnings[0].message


def test_sdk_available_false_warns_only_once_when_router_writes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from nygen_router import CallVariant, ProviderConfig, ProviderRouter

    class _Adapter:
        def invoke(self, operation: str, arguments: dict[str, object]) -> str:
            return "response"

    config = ProviderConfig(
        name="provider_a",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://provider-a.example.com/v1",
        api_key="secret",
    )
    calls = [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={"messages": []},
        )
    ]

    with caplog.at_level(logging.WARNING):
        store = DuckDBMetricsStore(sdk_available=False)
        router = ProviderRouter(
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
def test_query_recent_reads_back_recorded_events(tmp_path: Path) -> None:
    store = DuckDBMetricsStore(tmp_path / "metrics.duckdb")
    since = datetime.now(UTC) - timedelta(minutes=1)

    store.record_attempt(
        MetricsEvent(
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
