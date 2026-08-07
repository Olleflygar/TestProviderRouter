"""PR14A behavior that needs no PostgreSQL server, so it runs in every suite.

Configuration validation, credential redaction, the missing-driver path,
command-line argument handling, and router/policy degradation are all
observable without a database, and they are exactly the parts that must not
silently stop being covered on a machine with no test database configured.
"""

from __future__ import annotations

import importlib.util
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ConfigError,
    MetricsEvent,
    PostgresConfig,
    PostgresMetricsStore,
    PostgresPoolMode,
    ProviderConfig,
    ProviderRouter,
    RoutingContext,
    ScoreAggregateProvider,
    ScoreAggregateQuery,
    ScoreBasedPolicy,
    redact_postgres_url,
)
from nygen_router.cli import POSTGRES_URL_ENV, main
from nygen_router.storage.score_aggregation import FlatScoreWeighting

_PSYCOPG_AVAILABLE = importlib.util.find_spec("psycopg") is not None

# Reserved for documentation examples (RFC 5737); nothing listens there, so a
# connection attempt fails without reaching any real host.
UNREACHABLE_URL = "postgresql://user:hunter2@203.0.113.1:5432/postgres"

_FAST_FAIL = {"connect_timeout_seconds": 1.0, "checkout_timeout_seconds": 1.0}


def _connection_error_types() -> tuple[type[BaseException], ...]:
    """Either the driver refuses the connection or the pool gives up waiting."""
    if not _PSYCOPG_AVAILABLE:
        return (OSError,)
    import psycopg
    import psycopg_pool

    return (psycopg.Error, psycopg_pool.PoolTimeout)


_CONNECTION_ERRORS = _connection_error_types()


class TestConfiguration:
    def test_encryption_is_required_by_default(self) -> None:
        store = PostgresMetricsStore("postgresql://h/db")
        assert store.effective_sslmode == "require"

    def test_a_stronger_mode_in_the_url_is_not_downgraded(self) -> None:
        store = PostgresMetricsStore("postgresql://h/db?sslmode=verify-full")
        assert store.effective_sslmode == "verify-full"

    def test_explicit_configuration_beats_the_url(self) -> None:
        store = PostgresMetricsStore(
            "postgresql://h/db?sslmode=require", config={"sslmode": "verify-full"}
        )
        assert store.effective_sslmode == "verify-full"

    def test_an_unencrypted_url_is_refused_too(self) -> None:
        with pytest.raises(ConfigError, match="allow_unencrypted"):
            PostgresMetricsStore("postgresql://h/db?sslmode=disable")

    def test_libpq_default_prefer_is_treated_as_unencrypted(self) -> None:
        with pytest.raises(ConfigError, match="allow_unencrypted"):
            PostgresMetricsStore("postgresql://h/db?sslmode=prefer")

    def test_pool_mode_defaults_to_direct(self) -> None:
        assert PostgresConfig().pool_mode is PostgresPoolMode.DIRECT

    def test_default_timeouts_are_latency_first(self) -> None:
        config = PostgresConfig()
        assert config.connect_timeout_seconds == 5.0
        assert config.statement_timeout_seconds == 2.0
        assert config.checkout_timeout_seconds == 2.0

    def test_default_pool_is_small(self) -> None:
        config = PostgresConfig()
        assert config.min_pool_size == 1
        assert config.max_pool_size == 4

    def test_unencrypted_requires_an_explicit_opt_in(self) -> None:
        with pytest.raises(ConfigError, match="allow_unencrypted"):
            PostgresMetricsStore("postgresql://h/db", config={"sslmode": "disable"})

    def test_unencrypted_is_allowed_once_confirmed(self) -> None:
        store = PostgresMetricsStore(
            "postgresql://h/db", config={"sslmode": "disable", "allow_unencrypted": True}
        )
        assert store.effective_sslmode == "disable"

    def test_inverted_pool_bounds_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match="max_pool_size"):
            PostgresMetricsStore(
                "postgresql://h/db", config={"min_pool_size": 5, "max_pool_size": 2}
            )

    def test_non_positive_timeouts_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostgresMetricsStore("postgresql://h/db", config={"statement_timeout_seconds": 0})

    def test_unknown_settings_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostgresMetricsStore("postgresql://h/db", config={"pool_size": 4})

    def test_a_config_instance_is_accepted_as_well_as_a_mapping(self) -> None:
        store = PostgresMetricsStore(
            "postgresql://h/db", config=PostgresConfig(statement_timeout_seconds=9.0)
        )
        assert store.config.statement_timeout_seconds == 9.0

    def test_blank_url_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="non-blank"):
            PostgresMetricsStore("   ")


class TestCredentialRedaction:
    def test_the_password_never_appears_in_the_public_target(self) -> None:
        store = PostgresMetricsStore("postgresql://someone:sup3rsecret@db.example:5432/postgres")
        assert "sup3rsecret" not in store.target
        assert store.target == "postgresql://someone:***@db.example:5432/postgres"

    def test_redaction_keeps_the_diagnostic_parts(self) -> None:
        redacted = redact_postgres_url("postgresql://u:pw@host.example:6543/db?sslmode=require")
        assert "pw" not in redacted
        assert "host.example:6543" in redacted

    def test_a_url_without_credentials_is_unchanged(self) -> None:
        assert (
            redact_postgres_url("postgresql://host.example/postgres")
            == "postgresql://host.example/postgres"
        )

    def test_query_parameters_are_dropped_because_they_can_carry_secrets(self) -> None:
        assert "secret" not in redact_postgres_url("postgresql://h/db?password=secret")

    @pytest.mark.skipif(not _PSYCOPG_AVAILABLE, reason="psycopg is not installed")
    def test_connection_failures_do_not_leak_the_password(self) -> None:
        store = PostgresMetricsStore(UNREACHABLE_URL, config=_FAST_FAIL)
        try:
            with pytest.raises(_CONNECTION_ERRORS) as caught:
                store.query_recent(since=datetime.now(UTC) - timedelta(hours=1))
        finally:
            store.close()
        assert "hunter2" not in str(caught.value)


class TestMissingDriver:
    def test_construction_warns_and_does_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            store = PostgresMetricsStore("postgresql://h/db", driver_available=False)
        assert store.available is False
        assert "nygen-router[postgres]" in caplog.text

    def test_first_use_raises_an_actionable_import_error(self) -> None:
        store = PostgresMetricsStore("postgresql://h/db", driver_available=False)
        with pytest.raises(ImportError, match=r"nygen-router\[postgres\]"):
            store.record_attempt(_event())

    def test_close_before_any_use_is_harmless(self) -> None:
        PostgresMetricsStore("postgresql://h/db", driver_available=False).close()


class TestCommandLineArguments:
    def test_migrate_reports_that_no_route_exists(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(POSTGRES_URL_ENV, UNREACHABLE_URL)
        assert main(["storage", "migrate", "--backend", "postgres"]) == 4
        assert "no PostgreSQL migration route" in capsys.readouterr().err

    def test_a_missing_url_is_an_argument_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(POSTGRES_URL_ENV, raising=False)
        with pytest.raises(SystemExit) as caught:
            main(["storage", "inspect", "--backend", "postgres"])
        assert caught.value.code == 2

    def test_path_is_rejected_for_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(POSTGRES_URL_ENV, UNREACHABLE_URL)
        with pytest.raises(SystemExit) as caught:
            main(["storage", "inspect", "--backend", "postgres", "--path", "/tmp/x.sqlite"])
        assert caught.value.code == 2

    def test_url_is_rejected_for_a_local_backend(self) -> None:
        with pytest.raises(SystemExit) as caught:
            main(["storage", "inspect", "--backend", "sqlite", "--url", "postgresql://h/db"])
        assert caught.value.code == 2

    def test_a_local_backend_still_requires_a_target(self) -> None:
        with pytest.raises(SystemExit) as caught:
            main(["storage", "inspect", "--backend", "sqlite"])
        assert caught.value.code == 2

    def test_default_still_works_for_duckdb(self) -> None:
        # Regression guard: making the target group optional must not weaken
        # the shipped local argument handling.
        with pytest.raises(SystemExit) as caught:
            main(["storage", "inspect", "--backend", "sqlite", "--default"])
        assert caught.value.code == 2


class _ScriptedAdapter:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        return f"response from {self.config.provider_id}"


def _event() -> MetricsEvent:
    return MetricsEvent(
        metrics_scope="pr14a",
        provider_id="provider_a",
        provider_name="provider_a",
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        call_type=CallType.REGULAR,
        success=True,
        latency_ms=5.0,
    )


def _provider(provider_id: str) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=provider_id,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{provider_id}.example.com/v1",
        api_key="secret",
    )


def _calls() -> list[CallVariant]:
    return [
        CallVariant(
            call_type=CallType.REGULAR,
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={"messages": [{"role": "user", "content": "hi"}]},
        )
    ]


@pytest.mark.skipif(not _PSYCOPG_AVAILABLE, reason="psycopg is not installed")
class TestDegradationWithARealUnreachableStore:
    """An unreachable shared database must never cost the caller their response."""

    def test_invoke_still_returns_the_provider_response(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = PostgresMetricsStore(UNREACHABLE_URL, config=_FAST_FAIL)
        router = ProviderRouter(
            metrics_scope="test",
            providers=[_provider("provider_a")],
            adapter_factory=_ScriptedAdapter,
            metrics_store=store,
        )
        try:
            with caplog.at_level(logging.WARNING):
                response = router.invoke(_calls())
        finally:
            router.close()
            store.close()

        assert response == "response from provider_a"
        assert "Metrics storage is unavailable" in caplog.text

    def test_the_unavailability_warning_is_not_repeated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = PostgresMetricsStore(UNREACHABLE_URL, config=_FAST_FAIL)
        router = ProviderRouter(
            metrics_scope="test",
            providers=[_provider("provider_a")],
            adapter_factory=_ScriptedAdapter,
            metrics_store=store,
        )
        try:
            with caplog.at_level(logging.WARNING):
                router.invoke(_calls())
                router.invoke(_calls())
        finally:
            router.close()
            store.close()

        assert caplog.text.count("Metrics storage is unavailable") == 1

    def test_scoring_falls_back_to_the_tie_break_order(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = PostgresMetricsStore(UNREACHABLE_URL, config=_FAST_FAIL)
        policy = ScoreBasedPolicy()
        eligible = [_provider("provider_a"), _provider("provider_b")]
        context = RoutingContext(
            metrics_store=store, metrics_scope="test", call_type=CallType.REGULAR
        )
        try:
            with caplog.at_level(logging.WARNING):
                ordered = policy.order(list(eligible), context)
        finally:
            store.close()

        assert [item.provider_id for item in ordered] == ["provider_a", "provider_b"]
        assert "Metrics history is unavailable" in caplog.text

    def test_a_failed_aggregate_read_raises_out_of_the_store_itself(self) -> None:
        # The store surfaces the real backend error; the policy owns the
        # graceful fallback. Swallowing it here would hide a real outage.
        store = PostgresMetricsStore(UNREACHABLE_URL, config=_FAST_FAIL)
        query = ScoreAggregateQuery(
            providers=(
                ScoreAggregateProvider(
                    provider_id="a", model="model-a", protocol=ApiProtocol.OPENAI_CHAT
                ),
            ),
            metrics_scope="test",
            call_type=CallType.REGULAR,
            since=datetime.now(UTC) - timedelta(hours=1),
            reference_time=datetime.now(UTC),
            weighting=FlatScoreWeighting(),
        )
        try:
            with pytest.raises(_CONNECTION_ERRORS):
                store.query_score_aggregates(query)
        finally:
            store.close()
