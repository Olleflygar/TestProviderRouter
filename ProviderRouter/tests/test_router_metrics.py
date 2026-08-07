from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from metrics_store_helpers import zero_score_aggregates

from llm_provider_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    MetricsEvent,
    ProviderConfig,
    ProviderHTTPError,
    ProviderRouter,
    ProviderTimeoutError,
    RouterExhaustedError,
    RoutingContext,
    ScoreAggregate,
    ScoreAggregateQuery,
)


class _FakeStore:
    """In-memory MetricsStore fake, injected via metrics_store= (no monkeypatching)."""

    def __init__(self) -> None:
        self.events: list[MetricsEvent] = []

    def record_attempt(self, event: MetricsEvent) -> None:
        self.events.append(event)

    def query_recent(
        self,
        *,
        since: datetime,
        metrics_scope: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        protocol: ApiProtocol | None = None,
        call_type: CallType | None = None,
    ) -> list[MetricsEvent]:
        return list(self.events)

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        return zero_score_aggregates(query)


class _RaisingStore:
    """MetricsStore fake whose record_attempt always raises a given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def record_attempt(self, event: MetricsEvent) -> None:
        raise self._exc

    def query_recent(
        self,
        *,
        since: datetime,
        metrics_scope: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        protocol: ApiProtocol | None = None,
        call_type: CallType | None = None,
    ) -> list[MetricsEvent]:
        return []

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        return zero_score_aggregates(query)


class _RecoveringStore(_FakeStore):
    """Fail a fixed number of writes, then recover without changing stores."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self._failures_remaining = failures

    def record_attempt(self, event: MetricsEvent) -> None:
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise RuntimeError("temporary storage failure")
        super().record_attempt(event)


class _ScriptedAdapter:
    """Adapter whose per-provider behavior is scripted: raise an exception or succeed."""

    def __init__(self, config: ProviderConfig, behaviors: dict[str, Exception]):
        self.config = config
        self._behaviors = behaviors

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        behavior = self._behaviors.get(self.config.name)
        if behavior is not None:
            raise behavior
        return self.config.name


class _StaticPolicy:
    """Try eligible providers in config order (no rotation) for deterministic tests."""

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(eligible)


def _config(name: str, *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        provider_id=name,
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
        enabled=enabled,
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


def _router(
    providers: list[ProviderConfig],
    behaviors: dict[str, Exception],
    *,
    metrics_store: object,
) -> ProviderRouter:
    def factory(config: ProviderConfig) -> _ScriptedAdapter:
        return _ScriptedAdapter(config, behaviors)

    return ProviderRouter(
        metrics_scope="test",
        providers=providers,
        adapter_factory=factory,
        policy=_StaticPolicy(),
        metrics_store=metrics_store,  # type: ignore[arg-type]
    )


def test_successful_call_records_exactly_one_success_event() -> None:
    store = _FakeStore()
    router = _router([_config("provider_a")], {}, metrics_store=store)

    response = router.invoke(_calls())

    assert response == "provider_a"
    assert len(store.events) == 1
    event = store.events[0]
    assert event.provider_name == "provider_a"
    assert event.model == "model-a"
    assert event.protocol == ApiProtocol.OPENAI_CHAT
    assert event.success is True
    assert event.error_type is None
    assert event.latency_ms is not None
    assert event.latency_ms > 0


def test_fallback_records_two_events_in_attempt_order() -> None:
    store = _FakeStore()
    timeout = ProviderTimeoutError(
        "timed out", provider_id="provider_a", provider_name="provider_a", model="model-a"
    )
    router = _router(
        [_config("provider_a"), _config("provider_b")],
        {"provider_a": timeout},
        metrics_store=store,
    )

    response = router.invoke(_calls())

    assert response == "provider_b"
    assert len(store.events) == 2
    assert store.events[0].provider_name == "provider_a"
    assert store.events[0].success is False
    assert store.events[0].error_type == "timeout"
    assert store.events[1].provider_name == "provider_b"
    assert store.events[1].success is True


def test_excluded_providers_produce_no_events() -> None:
    store = _FakeStore()
    router = _router(
        [_config("provider_a", enabled=False), _config("provider_b")],
        {},
        metrics_store=store,
    )

    router.invoke(_calls())

    assert [event.provider_name for event in store.events] == ["provider_b"]


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ImportError("duckdb not installed")])
def test_store_raising_does_not_break_the_call(exc: Exception) -> None:
    store = _RaisingStore(exc)
    router = _router([_config("provider_a")], {}, metrics_store=store)

    response = router.invoke(_calls())

    assert response == "provider_a"


def test_repeated_storage_failures_warn_once_and_include_debug_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _RaisingStore(RuntimeError("sensitive diagnostic detail"))
    router = _router([_config("provider_a")], {}, metrics_store=store)

    with caplog.at_level(logging.DEBUG, logger="llm_provider_router.router"):
        router.invoke(_calls())
        router.invoke(_calls())

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    debug_records = [record for record in caplog.records if record.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert "sensitive diagnostic detail" not in warnings[0].message
    assert len(debug_records) == 2
    assert all(record.exc_info is not None for record in debug_records)


def test_storage_retries_and_logs_one_recovery_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _RecoveringStore(failures=2)
    router = _router([_config("provider_a")], {}, metrics_store=store)

    with caplog.at_level(logging.INFO, logger="llm_provider_router.router"):
        router.invoke(_calls())
        router.invoke(_calls())
        router.invoke(_calls())
        router.invoke(_calls())

    recovery_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.INFO and "recovered" in record.message
    ]
    assert recovery_messages == ["Metrics storage recovered after 2 unrecorded attempt(s)."]
    assert len(store.events) == 2


def test_metrics_store_none_records_nothing_and_creates_no_file(tmp_path: Path) -> None:
    router = _router([_config("provider_a")], {}, metrics_store=None)

    response = router.invoke(_calls())

    assert response == "provider_a"
    assert list(tmp_path.iterdir()) == []


def test_failure_events_recorded_before_router_exhausted_error_is_raised() -> None:
    store = _FakeStore()
    http_error = ProviderHTTPError(
        provider_id="provider_a",
        provider_name="provider_a",
        model="model-a",
        status_code=429,
        message="rate limited",
    )
    router = _router([_config("provider_a")], {"provider_a": http_error}, metrics_store=store)

    with pytest.raises(RouterExhaustedError):
        router.invoke(_calls())

    assert len(store.events) == 1
    assert store.events[0].success is False
    assert store.events[0].error_type == "rate_limit"
