from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    DuckDBMetricsStore,
    MetricsEvent,
    MetricsSchemaMismatchError,
    SQLiteMetricsStore,
)
from nygen_router.storage.admin import LocalBackend, create_database, inspect_database
from nygen_router.storage.base import event_to_params, event_to_record, record_to_event
from nygen_router.storage.schema import (
    CREATE_PROVIDER_ATTEMPTS_TABLE_SQL,
    METRICS_SCHEMA_VERSION,
    SCHEMA_VERSIONS_TABLE,
    SchemaState,
)

_DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None

StoreFactory = Callable[[Path], DuckDBMetricsStore | SQLiteMetricsStore]


def _sqlite_store(path: Path) -> SQLiteMetricsStore:
    return SQLiteMetricsStore(path)


def _duckdb_store(path: Path) -> DuckDBMetricsStore:
    return DuckDBMetricsStore(path)


@pytest.fixture(
    params=[
        pytest.param((LocalBackend.SQLITE, _sqlite_store, ".sqlite"), id="sqlite"),
        pytest.param(
            (LocalBackend.DUCKDB, _duckdb_store, ".duckdb"),
            id="duckdb",
            marks=pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed"),
        ),
    ]
)
def backend_case(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[LocalBackend, StoreFactory, Path]:
    backend, factory, suffix = request.param
    return backend, factory, tmp_path / f"metrics{suffix}"


def _event(*, event_id: str = "event-1", timestamp: datetime | None = None) -> MetricsEvent:
    return MetricsEvent(
        id=event_id,
        timestamp=datetime.now(UTC) if timestamp is None else timestamp,
        metrics_scope="scope-a",
        provider_id="provider-a",
        provider_name="Provider A",
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        call_type=CallType.STREAMING,
        success=False,
        stream_opened=False,
        latency_ms=None,
        total_duration_ms=10.5,
        error_type="timeout",
    )


def _execute(backend: LocalBackend, path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    if backend is LocalBackend.SQLITE:
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()
        return
    import duckdb

    connection = duckdb.connect(str(path))
    try:
        connection.execute(sql, list(params))
    finally:
        connection.close()


def _table_names(backend: LocalBackend, path: Path) -> tuple[str, ...]:
    if backend is LocalBackend.SQLITE:
        connection = sqlite3.connect(str(path))
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        finally:
            connection.close()
    else:
        import duckdb

        connection = duckdb.connect(str(path), read_only=True)
        try:
            rows = connection.execute("SHOW TABLES").fetchall()
        finally:
            connection.close()
    return tuple(str(row[0]) for row in rows)


def _create_implicit_baseline(backend: LocalBackend, path: Path, event: MetricsEvent) -> None:
    _execute(backend, path, CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
    placeholders = ", ".join("?" for _ in event_to_params(event))
    _execute(
        backend,
        path,
        f"INSERT INTO provider_attempts VALUES ({placeholders})",
        event_to_params(event),
    )


def test_normal_first_use_creates_current_version_at_exact_path(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    backend, factory, path = backend_case
    store = factory(path)

    assert store.query_recent(since=datetime.now(UTC) - timedelta(minutes=1)) == []
    store.close()

    inspection = inspect_database(backend, path)
    assert inspection.path == path.resolve()
    assert inspection.schema.state is SchemaState.CURRENT
    assert inspection.schema.metrics_version == METRICS_SCHEMA_VERSION
    assert _table_names(backend, path) == (SCHEMA_VERSIONS_TABLE, "provider_attempts")


def test_reopen_reuses_one_file_and_preserves_history(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    _, factory, path = backend_case
    event = _event()
    first = factory(path)
    first.record_attempt(event)
    first.close()
    files_after_first_use = tuple(sorted(item.name for item in path.parent.iterdir()))

    second = factory(path)
    assert second.query_recent(since=event.timestamp - timedelta(seconds=1)) == [event]
    second.close()

    assert tuple(sorted(item.name for item in path.parent.iterdir())) == files_after_first_use
    assert files_after_first_use == (path.name,)


def test_exact_implicit_baseline_is_reused_without_runtime_stamping(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    backend, factory, path = backend_case
    original = _event()
    _create_implicit_baseline(backend, path, original)
    assert inspect_database(backend, path).schema.state is SchemaState.IMPLICIT_BASELINE

    store = factory(path)
    second = _event(event_id="event-2")
    store.record_attempt(second)
    assert store.query_recent(since=original.timestamp - timedelta(seconds=1)) == [original, second]
    store.close()

    assert inspect_database(backend, path).schema.state is SchemaState.IMPLICIT_BASELINE
    assert _table_names(backend, path) == ("provider_attempts",)


def test_existing_empty_database_is_not_initialized(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    backend, factory, path = backend_case
    if backend is LocalBackend.SQLITE:
        sqlite3.connect(str(path)).close()
    else:
        import duckdb

        duckdb.connect(str(path)).close()
    before = path.read_bytes()

    store = factory(path)
    with pytest.raises(MetricsSchemaMismatchError, match="left untouched"):
        store.query_recent(since=datetime.now(UTC) - timedelta(minutes=1))

    assert path.read_bytes() == before
    assert _table_names(backend, path) == ()


def test_newer_metadata_is_rejected_without_modification(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    backend, factory, path = backend_case
    create_database(backend, path)
    _execute(
        backend,
        path,
        f"UPDATE {SCHEMA_VERSIONS_TABLE} SET version = 2 WHERE component = 'metrics'",
    )
    before = path.read_bytes()

    with pytest.raises(MetricsSchemaMismatchError, match="newer_than_installed"):
        factory(path).record_attempt(_event())

    assert path.read_bytes() == before
    inspection = inspect_database(backend, path)
    assert inspection.schema.state is SchemaState.NEWER
    assert inspection.schema.metrics_version == 2


def test_other_component_version_is_tolerated_and_preserved(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    backend, factory, path = backend_case
    create_database(backend, path)
    _execute(
        backend,
        path,
        f"INSERT INTO {SCHEMA_VERSIONS_TABLE} VALUES (?, ?)",
        ("health", 7),
    )

    store = factory(path)
    store.record_attempt(_event())
    store.close()

    inspection = inspect_database(backend, path)
    assert inspection.schema.state is SchemaState.CURRENT
    assert [(item.component, item.version) for item in inspection.schema.components] == [
        ("health", 7),
        ("metrics", 1),
    ]


def test_named_event_conversion_normalizes_utc_and_preserves_nullable_values() -> None:
    plus_two = datetime.now(UTC).astimezone(datetime.now().astimezone().tzinfo)
    event = _event(timestamp=plus_two)

    record = event_to_record(event)
    read_back = record_to_event(record)

    assert isinstance(record["timestamp"], datetime)
    assert record["timestamp"].utcoffset() == timedelta(0)  # type: ignore[union-attr]
    assert record["protocol"] == "openai_chat"
    assert record["call_type"] == "streaming"
    assert record["success"] is False
    assert record["stream_opened"] is False
    assert record["latency_ms"] is None
    assert read_back == event


def test_naive_event_timestamp_is_rejected_before_storage(
    backend_case: tuple[LocalBackend, StoreFactory, Path],
) -> None:
    _, factory, path = backend_case
    naive = datetime.now()  # noqa: DTZ005 -- deliberate ambiguous input

    with pytest.raises(ValueError, match="event timestamp must be timezone-aware"):
        factory(path).record_attempt(_event(timestamp=naive))

    assert not path.exists()
