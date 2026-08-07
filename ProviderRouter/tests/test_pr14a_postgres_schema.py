"""PR14A: PostgreSQL provisioning, validation, and the administration surface.

The router never creates or alters a remote schema. Provisioning is a
deliberate administrative act, and every state a runtime store can meet is
either the exact current schema or an actionable refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from postgres_helpers import (
    config_for_url,
    drop_schema,
    postgres_available,
    postgres_url,
    reset_schema,
    restore_schema,
    skip_reason,
)

from nygen_router import (
    ApiProtocol,
    CallType,
    MetricsEvent,
    MetricsSchemaMismatchError,
    PostgresMetricsStore,
)
from nygen_router.cli import POSTGRES_URL_ENV, main
from nygen_router.storage.admin import (
    StorageTargetError,
    create_postgres_database,
    inspect_postgres_database,
)
from nygen_router.storage.schema import (
    METRICS_SCHEMA_VERSION,
    POSTGRES_REQUIRED_METRICS_INDEXES,
    SCHEMA_VERSIONS_TABLE,
    SchemaState,
)

pytestmark = pytest.mark.skipif(not postgres_available(), reason=skip_reason())


@pytest.fixture
def url() -> str:
    resolved = postgres_url()
    assert resolved is not None
    yield resolved
    restore_schema(resolved)


def _event() -> MetricsEvent:
    return MetricsEvent(
        metrics_scope="pr14a-schema",
        provider_id="provider_a",
        provider_name="provider_a",
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        call_type=CallType.REGULAR,
        success=True,
        latency_ms=5.0,
    )


class TestProvisioning:
    def test_an_unprovisioned_database_reads_as_missing(self, url: str) -> None:
        drop_schema(url)
        result = inspect_postgres_database(url)
        assert result.schema.state is SchemaState.MISSING
        assert result.exists is False
        assert result.schema.compatible is False
        assert "storage create" in result.schema.next_action
        assert "never creates" in result.schema.next_action

    def test_creation_produces_the_current_schema(self, url: str) -> None:
        drop_schema(url)
        created = create_postgres_database(url)
        assert created.metrics_version == METRICS_SCHEMA_VERSION
        assert created.validation.schema.state is SchemaState.CURRENT
        assert created.validation.schema.compatible is True

    def test_creation_records_the_component_version(self, url: str) -> None:
        drop_schema(url)
        create_postgres_database(url)
        report = inspect_postgres_database(url).schema
        assert report.metrics_version == METRICS_SCHEMA_VERSION
        assert [(item.component, item.version) for item in report.components] == [
            ("metrics", METRICS_SCHEMA_VERSION)
        ]

    def test_creation_builds_the_measured_indexes(self, url: str) -> None:
        drop_schema(url)
        create_postgres_database(url)
        with psycopg.connect(url, connect_timeout=15) as connection:
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'provider_attempts'"
                ).fetchall()
            }
        for definition in POSTGRES_REQUIRED_METRICS_INDEXES:
            assert definition.name in names

    def test_creation_refuses_an_occupied_target(self, url: str) -> None:
        reset_schema(url)
        with pytest.raises(StorageTargetError, match="already exists"):
            create_postgres_database(url)

    def test_a_refused_creation_leaves_existing_rows_untouched(self, url: str) -> None:
        reset_schema(url)
        store = PostgresMetricsStore(url, config=config_for_url(url))
        try:
            store.record_attempt(_event())
            with pytest.raises(StorageTargetError):
                create_postgres_database(url)
            assert len(store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))) == 1
        finally:
            store.close()


class TestRuntimeValidation:
    def test_the_store_refuses_an_unprovisioned_database(self, url: str) -> None:
        drop_schema(url)
        store = PostgresMetricsStore(url, config=config_for_url(url))
        try:
            with pytest.raises(MetricsSchemaMismatchError) as caught:
                store.record_attempt(_event())
        finally:
            store.close()
        assert "PostgreSQL" in str(caught.value)
        assert "storage create" in str(caught.value)

    def test_a_refused_store_creates_nothing(self, url: str) -> None:
        drop_schema(url)
        store = PostgresMetricsStore(url, config=config_for_url(url))
        try:
            with pytest.raises(MetricsSchemaMismatchError):
                store.record_attempt(_event())
        finally:
            store.close()
        with psycopg.connect(url, connect_timeout=15) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                ).fetchall()
            }
        assert "provider_attempts" not in tables
        assert SCHEMA_VERSIONS_TABLE not in tables

    def test_a_foreign_table_is_unknown_not_an_implicit_baseline(self, url: str) -> None:
        # PostgreSQL never had a version-1 schema, so an unversioned table is
        # somebody else's, not a baseline this project can recognize.
        drop_schema(url)
        with psycopg.connect(url, connect_timeout=15) as connection:
            connection.execute("CREATE TABLE provider_attempts (id TEXT PRIMARY KEY)")
            connection.commit()
        try:
            report = inspect_postgres_database(url).schema
            assert report.state is SchemaState.UNKNOWN
            assert report.compatible is False
        finally:
            with psycopg.connect(url, connect_timeout=15) as connection:
                connection.execute("DROP TABLE IF EXISTS provider_attempts")
                connection.commit()

    def test_validation_happens_once_per_store_not_per_call(self, url: str) -> None:
        reset_schema(url)
        store = PostgresMetricsStore(url, config=config_for_url(url))
        try:
            assert store._schema_validated is False
            store.record_attempt(_event())
            assert store._schema_validated is True
            store.record_attempt(_event())
            assert store._schema_validated is True
        finally:
            store.close()


class TestCommandLine:
    def test_inspect_reports_a_missing_schema(
        self, url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drop_schema(url)
        monkeypatch.setenv(POSTGRES_URL_ENV, url)
        assert main(["storage", "inspect", "--backend", "postgres"]) == 0
        output = capsys.readouterr().out
        assert "Backend: postgres" in output
        assert "Schema state: missing" in output

    def test_create_then_inspect_reports_current(
        self, url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drop_schema(url)
        monkeypatch.setenv(POSTGRES_URL_ENV, url)
        assert main(["storage", "create", "--backend", "postgres"]) == 0
        assert main(["storage", "inspect", "--backend", "postgres"]) == 0
        output = capsys.readouterr().out
        assert "Schema state: current" in output
        assert "INSERT/SELECT" in output

    def test_create_refuses_an_occupied_target_with_the_incompatible_code(
        self, url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_schema(url)
        monkeypatch.setenv(POSTGRES_URL_ENV, url)
        assert main(["storage", "create", "--backend", "postgres"]) == 4

    def test_the_url_never_reaches_the_output(
        self, url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(POSTGRES_URL_ENV, url)
        main(["storage", "inspect", "--backend", "postgres"])
        captured = capsys.readouterr()
        password = url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[-1]
        assert password not in captured.out
        assert password not in captured.err
