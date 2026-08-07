"""PR14A: PostgresMetricsStore behavior against a real PostgreSQL database."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta, timezone

import psycopg
import psycopg_pool
import pytest
from metrics_store_helpers import aggregate_events_for_score_query
from postgres_helpers import (
    TEST_TRANSACTION_URL_ENV,
    clear_events,
    config_for_url,
    ensure_schema,
    postgres_available,
    postgres_url,
    reset_schema,
    shared_store,
    skip_reason,
    unencrypted_opt_in,
)

from llm_provider_router import (
    ApiProtocol,
    CallType,
    MetricsEvent,
    MetricsSchemaMismatchError,
    ScoreAggregateProvider,
    ScoreAggregateQuery,
)
from llm_provider_router.storage.postgres import (
    PostgresMetricsStore,
    PostgresPoolMode,
)
from llm_provider_router.storage.score_aggregation import (
    ExponentialScoreWeighting,
    FlatScoreWeighting,
)

pytestmark = pytest.mark.skipif(not postgres_available(), reason=skip_reason())

_SCOPE = "pr14a"


@pytest.fixture
def store() -> PostgresMetricsStore:
    """The process-wide store with an empty attempts table."""
    url = postgres_url()
    assert url is not None
    ensure_schema(url)
    clear_events(url)
    return shared_store()  # type: ignore[return-value]


def _event(
    *,
    provider_id: str = "provider_a",
    model: str = "model-a",
    success: bool = True,
    latency_ms: float | None = 12.5,
    error_type: str | None = None,
    call_type: CallType = CallType.REGULAR,
    stream_opened: bool | None = None,
    total_duration_ms: float | None = None,
    hours_ago: float = 0.0,
    timestamp: datetime | None = None,
) -> MetricsEvent:
    return MetricsEvent(
        metrics_scope=_SCOPE,
        provider_id=provider_id,
        provider_name=f"{provider_id} display",
        model=model,
        protocol=ApiProtocol.OPENAI_CHAT,
        call_type=call_type,
        success=success,
        latency_ms=latency_ms,
        error_type=error_type,
        stream_opened=stream_opened,
        total_duration_ms=total_duration_ms,
        timestamp=timestamp or (datetime.now(UTC) - timedelta(hours=hours_ago)),
    )


class TestRoundTrip:
    def test_every_field_survives_the_round_trip(self, store: PostgresMetricsStore) -> None:
        original = _event(
            success=False,
            latency_ms=None,
            error_type="timeout",
            call_type=CallType.STREAMING,
            stream_opened=True,
            total_duration_ms=987.25,
        )
        store.record_attempt(original)

        (stored,) = store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))
        assert stored.id == original.id
        assert stored.metrics_scope == original.metrics_scope
        assert stored.provider_id == original.provider_id
        assert stored.provider_name == original.provider_name
        assert stored.model == original.model
        assert stored.protocol is ApiProtocol.OPENAI_CHAT
        assert stored.call_type is CallType.STREAMING
        assert stored.success is False
        assert stored.stream_opened is True
        assert stored.latency_ms is None
        assert stored.total_duration_ms == pytest.approx(987.25)
        assert stored.error_type == "timeout"

    def test_non_utc_timestamps_come_back_as_utc(self, store: PostgresMetricsStore) -> None:
        stockholm = timezone(timedelta(hours=2))
        written = datetime.now(stockholm).replace(microsecond=0)
        store.record_attempt(_event(timestamp=written))

        (stored,) = store.query_recent(since=written - timedelta(minutes=1))
        assert stored.timestamp.tzinfo is not None
        assert stored.timestamp.utcoffset() == timedelta(0)
        assert stored.timestamp == written

    def test_nullable_observations_stay_null(self, store: PostgresMetricsStore) -> None:
        store.record_attempt(
            _event(latency_ms=None, total_duration_ms=None, stream_opened=None, error_type=None)
        )
        (stored,) = store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))
        assert stored.latency_ms is None
        assert stored.total_duration_ms is None
        assert stored.stream_opened is None
        assert stored.error_type is None


class TestBatchWrites:
    def test_many_rows_land_in_one_batch(self, store: PostgresMetricsStore) -> None:
        events = [_event(provider_id=f"p{index}") for index in range(25)]
        store.record_attempts(events)

        stored = store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))
        assert len(stored) == 25
        assert {event.provider_id for event in stored} == {f"p{index}" for index in range(25)}

    def test_a_failing_batch_leaves_nothing_behind(self, store: PostgresMetricsStore) -> None:
        good = _event(provider_id="kept")
        duplicate = _event(provider_id="duplicate")
        clash = MetricsEvent(
            id=duplicate.id,
            metrics_scope=_SCOPE,
            provider_id="duplicate",
            provider_name="duplicate",
            model="model-a",
            protocol=ApiProtocol.OPENAI_CHAT,
            call_type=CallType.REGULAR,
            success=True,
            latency_ms=1.0,
        )

        with pytest.raises(psycopg.errors.UniqueViolation):
            store.record_attempts([good, duplicate, clash])

        assert store.query_recent(since=datetime.now(UTC) - timedelta(hours=1)) == []

    def test_an_empty_batch_is_a_no_op(self, store: PostgresMetricsStore) -> None:
        store.record_attempts([])
        assert store.query_recent(since=datetime.now(UTC) - timedelta(hours=1)) == []

    def test_single_write_goes_through_the_batch_path(self, store: PostgresMetricsStore) -> None:
        # record_attempt delegates to record_attempts, so the batch path is
        # exercised by every ordinary write rather than shipping unused.
        store.record_attempt(_event())
        assert len(store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))) == 1


class TestConcurrency:
    def test_concurrent_writers_lose_nothing(self, store: PostgresMetricsStore) -> None:
        writers, per_writer = 6, 5
        barrier = threading.Barrier(writers)
        errors: list[BaseException] = []

        def write(index: int) -> None:
            try:
                barrier.wait(timeout=30)
                for _ in range(per_writer):
                    store.record_attempt(_event(provider_id=f"writer{index}"))
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        stored = store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))
        assert len(stored) == writers * per_writer

    def test_reads_and_writes_interleave_from_several_threads(
        self, store: PostgresMetricsStore
    ) -> None:
        errors: list[BaseException] = []

        def churn() -> None:
            try:
                for _ in range(4):
                    store.record_attempt(_event())
                    store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=churn) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        assert len(store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))) == 16


class TestAggregateEquivalence:
    @pytest.mark.parametrize(
        "weighting",
        [FlatScoreWeighting(), ExponentialScoreWeighting(half_life_hours=1.5)],
        ids=["flat", "exponential"],
    )
    @pytest.mark.parametrize("scope", [_SCOPE, None], ids=["current-scope", "all-scopes"])
    def test_backend_matches_the_pure_reference(
        self,
        store: PostgresMetricsStore,
        weighting: FlatScoreWeighting | ExponentialScoreWeighting,
        scope: str | None,
    ) -> None:
        now = datetime.now(UTC)
        events = [
            _event(provider_id="a", success=True, latency_ms=100.0, hours_ago=0.0),
            _event(provider_id="a", success=True, latency_ms=300.0, hours_ago=1.5),
            _event(
                provider_id="a", success=False, latency_ms=None, error_type="timeout", hours_ago=2.0
            ),
            _event(
                provider_id="a",
                success=False,
                latency_ms=None,
                error_type="rate_limit",
                hours_ago=2.5,
            ),
            _event(provider_id="a", success=True, latency_ms=None, hours_ago=3.0),
            _event(provider_id="b", success=True, latency_ms=50.0, hours_ago=0.5),
            _event(
                provider_id="b",
                success=False,
                latency_ms=None,
                error_type="server_error",
                hours_ago=1.0,
            ),
            _event(
                provider_id="a",
                success=True,
                latency_ms=999.0,
                hours_ago=0.5,
                call_type=CallType.STREAMING,
            ),
        ]
        store.record_attempts(events)

        providers = tuple(
            ScoreAggregateProvider(
                provider_id=provider_id, model="model-a", protocol=ApiProtocol.OPENAI_CHAT
            )
            for provider_id in ("a", "b", "never-seen")
        )
        query = ScoreAggregateQuery(
            providers=providers,
            metrics_scope=scope,
            call_type=CallType.REGULAR,
            since=now - timedelta(hours=6),
            reference_time=now,
            weighting=weighting,
        )

        actual = store.query_score_aggregates(query)
        expected = aggregate_events_for_score_query(events, query)

        assert len(actual) == len(providers)
        for got, want in zip(actual, expected, strict=True):
            assert got.provider_id == want.provider_id
            assert got.attempt_weight == pytest.approx(want.attempt_weight)
            assert got.success_weight == pytest.approx(want.success_weight)
            assert got.successful_latency_weight == pytest.approx(want.successful_latency_weight)
            assert got.successful_latency_total_ms == pytest.approx(
                want.successful_latency_total_ms
            )
            assert got.recent_error_count == want.recent_error_count
            assert got.rate_limit_count == want.rate_limit_count
            assert got.timeout_count == want.timeout_count

    def test_a_provider_with_no_history_gets_an_explicit_zero_row(
        self, store: PostgresMetricsStore
    ) -> None:
        now = datetime.now(UTC)
        query = ScoreAggregateQuery(
            providers=(
                ScoreAggregateProvider(
                    provider_id="fresh", model="model-a", protocol=ApiProtocol.OPENAI_CHAT
                ),
            ),
            metrics_scope=_SCOPE,
            call_type=CallType.REGULAR,
            since=now - timedelta(hours=1),
            reference_time=now,
            weighting=FlatScoreWeighting(),
        )
        (row,) = store.query_score_aggregates(query)
        assert row.provider_id == "fresh"
        assert row.attempt_weight == 0.0
        assert row.success_weight == 0.0
        assert row.recent_error_count == 0

    def test_one_row_per_requested_provider_regardless_of_history_size(
        self, store: PostgresMetricsStore
    ) -> None:
        store.record_attempts([_event(provider_id="a") for _ in range(40)])
        now = datetime.now(UTC)
        providers = tuple(
            ScoreAggregateProvider(
                provider_id=provider_id, model="model-a", protocol=ApiProtocol.OPENAI_CHAT
            )
            for provider_id in ("a", "b", "c")
        )
        rows = store.query_score_aggregates(
            ScoreAggregateQuery(
                providers=providers,
                metrics_scope=_SCOPE,
                call_type=CallType.REGULAR,
                since=now - timedelta(hours=1),
                reference_time=now,
                weighting=FlatScoreWeighting(),
            )
        )
        assert [row.provider_id for row in rows] == ["a", "b", "c"]


class TestConnectionSettings:
    def test_the_shipped_default_statement_timeout_reaches_the_server(self) -> None:
        url = postgres_url()
        assert url is not None
        # Keep shipped timeout defaults; only opt into CI's deliberate plaintext URL.
        default_store = PostgresMetricsStore(url, config=unencrypted_opt_in(url))
        try:
            with default_store._connection(validate=False) as connection:
                (value,) = connection.execute("SHOW statement_timeout").fetchone()
        finally:
            default_store.close()
        assert value == "2s"

    def test_a_raised_statement_timeout_is_honored(self, store: PostgresMetricsStore) -> None:
        with store._connection() as connection:
            (value,) = connection.execute("SHOW statement_timeout").fetchone()
        assert value == "5s"

    def test_statement_timeout_actually_cancels_a_slow_query(self) -> None:
        # A managed pooler accepts the connection's startup options and
        # silently ignores them, so this asserts real enforcement rather than
        # merely that a setting was requested.
        url = postgres_url()
        assert url is not None
        impatient = PostgresMetricsStore(
            url,
            config={
                "statement_timeout_seconds": 0.5,
                "checkout_timeout_seconds": 10.0,
                **unencrypted_opt_in(url),
            },
        )
        try:
            with pytest.raises(psycopg.errors.QueryCanceled) as caught:
                with impatient._connection(validate=False) as connection:
                    connection.execute("SELECT pg_sleep(5)")
        finally:
            impatient.close()
        assert "canceling statement" in str(caught.value).lower()

    def test_connect_failure_surfaces_within_the_configured_bound(self) -> None:
        unreachable = "postgresql://user:pw@203.0.113.1:5432/postgres"
        store = PostgresMetricsStore(
            unreachable, config={"connect_timeout_seconds": 2.0, "checkout_timeout_seconds": 2.0}
        )
        started = datetime.now(UTC)
        try:
            with pytest.raises((psycopg.Error, psycopg_pool.PoolTimeout)):
                store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))
        finally:
            store.close()
        assert (datetime.now(UTC) - started).total_seconds() < 30

    def test_close_is_idempotent_and_reconnects(self, store: PostgresMetricsStore) -> None:
        store.record_attempt(_event())
        store.close()
        store.close()
        assert len(store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))) == 1

    def test_context_manager_closes_the_pool(self) -> None:
        url = postgres_url()
        assert url is not None
        reset_schema(url)
        with PostgresMetricsStore(url, config=config_for_url(url)) as store:
            store.record_attempt(_event())
        assert store._pool_instance is None


class TestTransactionPoolerMode:
    def test_transaction_pooler_mode_works_end_to_end(self) -> None:
        url = postgres_url(TEST_TRANSACTION_URL_ENV)
        if url is None:
            pytest.skip(f"{TEST_TRANSACTION_URL_ENV} is not configured")
        primary = postgres_url()
        assert primary is not None
        ensure_schema(primary)
        clear_events(primary)

        store = PostgresMetricsStore(
            url,
            config={"pool_mode": PostgresPoolMode.TRANSACTION_POOLER, **config_for_url(url)},
        )
        try:
            for _ in range(8):
                store.record_attempt(_event())
            stored = store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))
        finally:
            store.close()
        assert len(stored) == 8

    def test_transaction_pooler_mode_disables_prepared_statements(self) -> None:
        url = postgres_url(TEST_TRANSACTION_URL_ENV)
        if url is None:
            pytest.skip(f"{TEST_TRANSACTION_URL_ENV} is not configured")
        store = PostgresMetricsStore(
            url,
            config={"pool_mode": PostgresPoolMode.TRANSACTION_POOLER, **config_for_url(url)},
        )
        try:
            with store._connection(validate=False) as connection:
                assert connection.prepare_threshold is None
        finally:
            store.close()


class TestCredentialSafety:
    def test_schema_mismatch_errors_carry_no_password(self) -> None:
        url = postgres_url()
        assert url is not None
        from postgres_helpers import drop_schema

        drop_schema(url)
        store = PostgresMetricsStore(url, config=config_for_url(url))
        try:
            with pytest.raises(MetricsSchemaMismatchError) as caught:
                store.record_attempt(_event())
        finally:
            store.close()
            reset_schema(url)
        message = str(caught.value)
        password = url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[-1]
        assert password not in message

    def test_the_store_reports_the_driver_as_available(self, store: PostgresMetricsStore) -> None:
        assert store.available is True
