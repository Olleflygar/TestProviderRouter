from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from nygen_router import (
    ApiProtocol,
    CallVariant,
    MetricsEvent,
    ProviderConfig,
    ProviderHTTPError,
    ProviderRouter,
    ProviderTimeoutError,
    RouterExhaustedError,
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
        provider_name: str | None = None,
        model: str | None = None,
    ) -> list[MetricsEvent]:
        return list(self.events)


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
        provider_name: str | None = None,
        model: str | None = None,
    ) -> list[MetricsEvent]:
        return []


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

    def order(self, eligible: list[ProviderConfig]) -> list[ProviderConfig]:
        return list(eligible)


def _config(name: str, *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
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
    timeout = ProviderTimeoutError("timed out", provider_name="provider_a", model="model-a")
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


def test_metrics_store_none_records_nothing_and_creates_no_file(tmp_path: Path) -> None:
    router = _router([_config("provider_a")], {}, metrics_store=None)

    response = router.invoke(_calls())

    assert response == "provider_a"
    assert list(tmp_path.iterdir()) == []


def test_failure_events_recorded_before_router_exhausted_error_is_raised() -> None:
    store = _FakeStore()
    http_error = ProviderHTTPError(
        provider_name="provider_a", model="model-a", status_code=429, message="rate limited"
    )
    router = _router([_config("provider_a")], {"provider_a": http_error}, metrics_store=store)

    with pytest.raises(RouterExhaustedError):
        router.invoke(_calls())

    assert len(store.events) == 1
    assert store.events[0].success is False
    assert store.events[0].error_type == "rate_limit"
