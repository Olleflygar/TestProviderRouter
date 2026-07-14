from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nygen_router import ApiProtocol, MetricsEvent, SQLiteMetricsStore


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
