"""Shared MetricsStore protocol-conformance suite.

Parametrized over both backends via a store-factory fixture -- the
parametrization is the only backend-specific part of this file. To check a
custom backend against the same contract, add a factory that constructs your
implementation and parametrize the `store` fixture with it (see README.md's
"bring your own backend" section).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nygen_router import ApiProtocol, MetricsEvent, MetricsStore, SQLiteMetricsStore

_DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None

StoreFactory = Callable[[Path], MetricsStore]


def _sqlite_factory(tmp_path: Path) -> MetricsStore:
    return SQLiteMetricsStore(tmp_path / "metrics.sqlite")


def _duckdb_factory(tmp_path: Path) -> MetricsStore:
    from nygen_router import DuckDBMetricsStore

    return DuckDBMetricsStore(tmp_path / "metrics.duckdb")


@pytest.fixture(
    params=[
        pytest.param(_sqlite_factory, id="sqlite"),
        pytest.param(
            _duckdb_factory,
            id="duckdb",
            marks=pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed"),
        ),
    ]
)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> MetricsStore:
    factory: StoreFactory = request.param
    return factory(tmp_path)


def _event(
    *,
    provider_name: str = "provider_a",
    model: str = "model-a",
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
    success: bool = True,
    latency_ms: float | None = 12.5,
    error_type: str | None = None,
    stream: bool = False,
    total_duration_ms: float | None = None,
    timestamp: datetime | None = None,
) -> MetricsEvent:
    return MetricsEvent(
        provider_name=provider_name,
        model=model,
        protocol=protocol,
        success=success,
        latency_ms=latency_ms,
        error_type=error_type,
        stream=stream,
        total_duration_ms=total_duration_ms,
        timestamp=timestamp if timestamp is not None else datetime.now(UTC),
    )


def test_schema_created_on_first_use(store: MetricsStore) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)

    events = store.query_recent(since=since)

    assert events == []


def test_records_and_reads_back_success_event_field_for_field(store: MetricsStore) -> None:
    event = _event(
        provider_name="provider_a", model="model-a", protocol=ApiProtocol.OPENAI_CHAT, success=True
    )

    store.record_attempt(event)
    since = event.timestamp - timedelta(seconds=1)
    (read_back,) = store.query_recent(since=since)

    assert read_back.id == event.id
    assert read_back.provider_name == event.provider_name
    assert read_back.model == event.model
    assert read_back.protocol == event.protocol
    assert read_back.success is True
    assert read_back.latency_ms == pytest.approx(event.latency_ms)
    assert read_back.error_type is None
    assert read_back.timestamp == event.timestamp


def test_records_and_reads_back_a_stream_event(store: MetricsStore) -> None:
    """On a stream row latency_ms is time-to-first-chunk and total_duration_ms is open-to-end."""
    event = _event(stream=True, latency_ms=8.0, total_duration_ms=1200.0)

    store.record_attempt(event)
    (read_back,) = store.query_recent(since=event.timestamp - timedelta(seconds=1))

    assert read_back.stream is True
    assert read_back.latency_ms == pytest.approx(8.0)
    assert read_back.total_duration_ms == pytest.approx(1200.0)


def test_non_stream_event_reads_back_with_stream_false_and_no_total_duration(
    store: MetricsStore,
) -> None:
    event = _event()

    store.record_attempt(event)
    (read_back,) = store.query_recent(since=event.timestamp - timedelta(seconds=1))

    assert read_back.stream is False
    assert read_back.total_duration_ms is None


def test_stream_event_with_no_first_chunk_round_trips_a_null_latency(store: MetricsStore) -> None:
    """A stream that died before its first chunk has no TTFT to report, and must not fake one."""
    event = _event(
        success=False,
        error_type="stream_interrupted",
        stream=True,
        latency_ms=None,
        total_duration_ms=45.0,
    )

    store.record_attempt(event)
    (read_back,) = store.query_recent(since=event.timestamp - timedelta(seconds=1))

    assert read_back.latency_ms is None
    assert read_back.total_duration_ms == pytest.approx(45.0)


def test_records_failure_event_error_type_round_trips_as_category_string(
    store: MetricsStore,
) -> None:
    event = _event(success=False, error_type="timeout")

    store.record_attempt(event)
    since = event.timestamp - timedelta(seconds=1)
    (read_back,) = store.query_recent(since=since)

    assert read_back.success is False
    assert read_back.error_type == "timeout"


def test_query_recent_returns_events_in_chronological_ascending_order(
    store: MetricsStore,
) -> None:
    base = datetime.now(UTC) - timedelta(minutes=10)
    first = _event(provider_name="first", timestamp=base)
    second = _event(provider_name="second", timestamp=base + timedelta(seconds=1))
    third = _event(provider_name="third", timestamp=base + timedelta(seconds=2))

    # Record out of order to prove the store sorts, not just preserves insertion order.
    store.record_attempt(third)
    store.record_attempt(first)
    store.record_attempt(second)

    events = store.query_recent(since=base - timedelta(seconds=1))

    assert [event.provider_name for event in events] == ["first", "second", "third"]


def test_query_recent_excludes_events_older_than_since(store: MetricsStore) -> None:
    now = datetime.now(UTC)
    old_event = _event(provider_name="old", timestamp=now - timedelta(hours=2))
    recent_event = _event(provider_name="recent", timestamp=now)

    store.record_attempt(old_event)
    store.record_attempt(recent_event)

    events = store.query_recent(since=now - timedelta(hours=1))

    assert [event.provider_name for event in events] == ["recent"]


def test_query_recent_filters_by_provider_name(store: MetricsStore) -> None:
    since = datetime.now(UTC) - timedelta(minutes=1)
    store.record_attempt(_event(provider_name="provider_a"))
    store.record_attempt(_event(provider_name="provider_b"))

    events = store.query_recent(since=since, provider_name="provider_a")

    assert [event.provider_name for event in events] == ["provider_a"]


def test_query_recent_filters_by_model(store: MetricsStore) -> None:
    since = datetime.now(UTC) - timedelta(minutes=1)
    store.record_attempt(_event(model="model-a"))
    store.record_attempt(_event(model="model-b"))

    events = store.query_recent(since=since, model="model-b")

    assert [event.model for event in events] == ["model-b"]


def test_query_recent_raises_value_error_for_naive_since(store: MetricsStore) -> None:
    naive_since = datetime.now()  # noqa: DTZ005 -- deliberately naive, to test the guard

    with pytest.raises(ValueError, match="timezone-aware"):
        store.query_recent(since=naive_since)


def test_timestamps_round_trip_as_timezone_aware_utc(store: MetricsStore) -> None:
    event = _event()

    store.record_attempt(event)
    (read_back,) = store.query_recent(since=event.timestamp - timedelta(seconds=1))

    assert read_back.timestamp.tzinfo is not None
    assert read_back.timestamp.utcoffset() == timedelta(0)
    assert read_back.timestamp == event.timestamp


def test_ids_are_unique_across_events(store: MetricsStore) -> None:
    since = datetime.now(UTC) - timedelta(minutes=1)
    store.record_attempt(_event())
    store.record_attempt(_event())

    events = store.query_recent(since=since)

    assert len({event.id for event in events}) == 2


def test_close_is_idempotent(store: MetricsStore) -> None:
    store.record_attempt(_event())

    store.close()
    store.close()  # must not raise
