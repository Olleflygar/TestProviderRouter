from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_provider_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    DuckDBMetricsStore,
    MetricsEvent,
    MetricsStore,
    ProviderConfig,
    ProviderRouter,
    SQLiteMetricsStore,
)
from llm_provider_router.storage.base import COLUMN_NAMES

_DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None


def _sqlite(path: Path) -> MetricsStore:
    return SQLiteMetricsStore(path / "metrics.sqlite")


def _duckdb(path: Path) -> MetricsStore:
    return DuckDBMetricsStore(path / "metrics.duckdb")


@pytest.fixture(
    params=[
        pytest.param(_sqlite, id="sqlite"),
        pytest.param(
            _duckdb,
            id="duckdb",
            marks=pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed"),
        ),
    ]
)
def pr29_store(request: pytest.FixtureRequest, tmp_path: Path) -> MetricsStore:
    factory: Callable[[Path], MetricsStore] = request.param
    return factory(tmp_path)


def _event(
    *,
    scope: str = "scope-a",
    provider_id: str = "provider-a",
    model: str = "model-a",
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
    call_type: CallType = CallType.REGULAR,
    timestamp: datetime | None = None,
) -> MetricsEvent:
    return MetricsEvent(
        metrics_scope=scope,
        provider_id=provider_id,
        provider_name="Same display",
        model=model,
        protocol=protocol,
        call_type=call_type,
        success=True,
        stream_opened=None if call_type is CallType.REGULAR else True,
        latency_ms=5.0,
        timestamp=datetime.now(UTC) if timestamp is None else timestamp,
    )


def test_every_query_dimension_filters_independently_and_combines(
    pr29_store: MetricsStore,
) -> None:
    base = datetime.now(UTC) - timedelta(seconds=10)
    events = [
        _event(timestamp=base),
        _event(scope="scope-b", timestamp=base + timedelta(seconds=1)),
        _event(provider_id="provider-b", timestamp=base + timedelta(seconds=2)),
        _event(model="model-b", timestamp=base + timedelta(seconds=3)),
        _event(
            protocol=ApiProtocol.OPENAI_RESPONSES,
            timestamp=base + timedelta(seconds=4),
        ),
        _event(call_type=CallType.STREAMING, timestamp=base + timedelta(seconds=5)),
    ]
    for event in events:
        pr29_store.record_attempt(event)
    since = base - timedelta(seconds=1)

    assert len(pr29_store.query_recent(since=since, metrics_scope=None)) == 6
    assert all(
        event.metrics_scope == "scope-b"
        for event in pr29_store.query_recent(since=since, metrics_scope="scope-b")
    )
    assert all(
        event.provider_id == "provider-b"
        for event in pr29_store.query_recent(since=since, provider_id="provider-b")
    )
    assert all(
        event.model == "model-b" for event in pr29_store.query_recent(since=since, model="model-b")
    )
    assert all(
        event.protocol is ApiProtocol.OPENAI_RESPONSES
        for event in pr29_store.query_recent(since=since, protocol=ApiProtocol.OPENAI_RESPONSES)
    )
    assert all(
        event.call_type is CallType.STREAMING
        for event in pr29_store.query_recent(since=since, call_type=CallType.STREAMING)
    )
    assert pr29_store.query_recent(
        since=since,
        metrics_scope="scope-a",
        provider_id="provider-a",
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        call_type=CallType.REGULAR,
    ) == [events[0]]


def test_new_sqlite_and_duckdb_tables_have_the_exact_shared_column_order(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "metrics.sqlite"
    sqlite_store = SQLiteMetricsStore(sqlite_path)
    sqlite_store.query_recent(since=datetime.now(UTC) - timedelta(seconds=1))
    sqlite_store.close()
    sqlite_connection = sqlite3.connect(str(sqlite_path))
    try:
        sqlite_columns = sqlite_connection.execute(
            "PRAGMA table_info('provider_attempts')"
        ).fetchall()
    finally:
        sqlite_connection.close()
    assert tuple(row[1] for row in sqlite_columns) == COLUMN_NAMES

    if not _DUCKDB_AVAILABLE:
        return
    import duckdb

    duckdb_path = tmp_path / "metrics.duckdb"
    duckdb_store = DuckDBMetricsStore(duckdb_path)
    duckdb_store.query_recent(since=datetime.now(UTC) - timedelta(seconds=1))
    duckdb_store.close()
    duckdb_connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        duckdb_columns = duckdb_connection.execute(
            "PRAGMA table_info('provider_attempts')"
        ).fetchall()
    finally:
        duckdb_connection.close()
    assert tuple(row[1] for row in duckdb_columns) == COLUMN_NAMES


def test_router_success_survives_incompatible_store_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE provider_attempts (id TEXT PRIMARY KEY, old_value TEXT)")
    connection.execute("INSERT INTO provider_attempts VALUES (?, ?)", ("legacy", "keep-me"))
    connection.commit()
    connection.close()

    class _Adapter:
        def invoke(self, operation: str, arguments: dict[str, object]) -> object:
            return response

    response = object()
    provider = ProviderConfig(
        provider_id="provider-a",
        name="Provider A",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://provider.example.com/v1",
        api_key="secret",
    )
    router = ProviderRouter(
        [provider],
        metrics_scope="test",
        metrics_store=SQLiteMetricsStore(path),
        adapter_factory=lambda _: _Adapter(),
    )

    assert (
        router.invoke(
            [
                CallVariant(
                    protocol=ApiProtocol.OPENAI_CHAT,
                    operation="chat.completions.create",
                    call_type=CallType.REGULAR,
                    arguments={"messages": []},
                )
            ]
        )
        is response
    )

    inspection = sqlite3.connect(str(path))
    try:
        columns = inspection.execute("PRAGMA table_info('provider_attempts')").fetchall()
        rows = inspection.execute("SELECT * FROM provider_attempts").fetchall()
    finally:
        inspection.close()
    assert [row[1] for row in columns] == ["id", "old_value"]
    assert rows == [("legacy", "keep-me")]
