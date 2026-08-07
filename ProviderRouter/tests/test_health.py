from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pytest
from metrics_store_helpers import zero_score_aggregates
from pydantic import ValidationError

from llm_provider_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ConfigError,
    FilterReason,
    HealthConfig,
    MetricsEvent,
    NoEligibleProvidersError,
    ProviderConfig,
    ProviderConnectionError,
    ProviderHealthReport,
    ProviderHTTPError,
    ProviderRouter,
    ProviderTimeoutError,
    RouterExhaustedError,
    RoutingContext,
    ScoreAggregate,
    ScoreAggregateQuery,
    UnsupportedOperationError,
)
from llm_provider_router.errors import ErrorCategory, categorize_error
from llm_provider_router.health import ProviderHealthState


class _FakeClock:
    """Controllable stand-in for time.monotonic, injected via the clock= seam.

    Starts well above zero so a test can never pass by accident on an
    uninitialised 0.0 default.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


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


class _Script:
    """Per-provider behavior for successive attempts, plus a record of what ran.

    Each provider gets a queue of behaviors consumed one per attempt: an
    exception to raise, or None to succeed. An exhausted queue succeeds, so a
    test only scripts the failures it cares about.
    """

    def __init__(self, behaviors: dict[str, list[Exception | None]]) -> None:
        self._behaviors = {name: list(queue) for name, queue in behaviors.items()}
        self.invoked: list[str] = []
        self.adapters_built: list[str] = []

    def next_for(self, name: str) -> Exception | None:
        self.invoked.append(name)
        queue = self._behaviors.get(name)
        if not queue:
            return None
        return queue.pop(0)


class _ScriptedAdapter:
    def __init__(self, config: ProviderConfig, script: _Script) -> None:
        self.config = config
        self._script = script

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        behavior = self._script.next_for(self.config.name)
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


def _timeout(name: str, message: str = "gateway timed out") -> ProviderTimeoutError:
    return ProviderTimeoutError(message, provider_id=name, provider_name=name, model="model-a")


def _connection(name: str, message: str = "could not connect") -> ProviderConnectionError:
    return ProviderConnectionError(message, provider_id=name, provider_name=name, model="model-a")


def _http(name: str, status: int, message: str = "provider said no") -> ProviderHTTPError:
    return ProviderHTTPError(
        provider_id=name, provider_name=name, model="model-a", status_code=status, message=message
    )


def _router(
    providers: list[ProviderConfig],
    script: _Script,
    *,
    clock: _FakeClock,
    health: object = None,
    metrics_store: object = None,
) -> ProviderRouter:
    def factory(config: ProviderConfig) -> _ScriptedAdapter:
        script.adapters_built.append(config.name)
        return _ScriptedAdapter(config, script)

    return ProviderRouter(
        metrics_scope="test",
        providers=providers,
        adapter_factory=factory,
        policy=_StaticPolicy(),
        metrics_store=metrics_store,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        clock=clock,
    )


def _fail(router: ProviderRouter, times: int = 1) -> None:
    """Drive N failing calls, absorbing the expected exhaustion errors."""
    for _ in range(times):
        with pytest.raises(RouterExhaustedError):
            router.invoke(_calls())


# --- state transitions (exercised directly on the dataclass) -----------------


def test_rate_limit_benches_provider_and_leaves_failure_count_untouched() -> None:
    """A 429 is flow control, not "provider is off": it benches without counting."""
    state = ProviderHealthState()
    config = HealthConfig(rate_limit_cooldown_seconds=30.0)

    started_bench = state.record_failure(ErrorCategory.RATE_LIMIT, "429 slow down", config, 100.0)

    assert started_bench is True
    assert state.cooldown_until == 130.0
    assert state.consecutive_failures == 0
    assert state.last_error == "429 slow down"


@pytest.mark.parametrize(
    "categories",
    [
        [ErrorCategory.TIMEOUT, ErrorCategory.TIMEOUT, ErrorCategory.TIMEOUT],
        [ErrorCategory.SERVER_ERROR, ErrorCategory.CONNECTION, ErrorCategory.UNKNOWN],
        [ErrorCategory.CONNECTION, ErrorCategory.TIMEOUT, ErrorCategory.SERVER_ERROR],
        [ErrorCategory.UNKNOWN, ErrorCategory.UNKNOWN, ErrorCategory.CONNECTION],
    ],
)
def test_three_counted_failures_bench_for_failure_cooldown(
    categories: list[ErrorCategory],
) -> None:
    state = ProviderHealthState()
    config = HealthConfig(failure_cooldown_seconds=45.0, failure_threshold=3)

    benched = [state.record_failure(cat, f"{cat} happened", config, 100.0) for cat in categories]

    assert benched == [False, False, True]  # only the third crosses the threshold
    assert state.consecutive_failures == 3
    assert state.cooldown_until == 145.0
    assert state.last_error == f"{categories[-1]} happened"


@pytest.mark.parametrize(
    "category",
    [ErrorCategory.RATE_LIMIT, ErrorCategory.AUTH],
)
def test_non_counted_categories_do_not_increment_the_counter(category: ErrorCategory) -> None:
    state = ProviderHealthState()

    state.record_failure(category, "boom", HealthConfig(), 100.0)

    assert state.consecutive_failures == 0


@pytest.mark.parametrize(
    "category",
    [ErrorCategory.BAD_REQUEST, ErrorCategory.INVALID_OPERATION],
)
def test_stop_categories_leave_health_untouched(category: ErrorCategory) -> None:
    """The router never reports these; if one arrived anyway it must not blame the provider."""
    state = ProviderHealthState()

    started_bench = state.record_failure(category, "boom", HealthConfig(), 100.0)

    assert started_bench is False
    assert state == ProviderHealthState()


def test_auth_failure_benches_for_the_run_without_counting() -> None:
    state = ProviderHealthState()

    started_bench = state.record_failure(ErrorCategory.AUTH, "401 bad key", HealthConfig(), 100.0)

    assert started_bench is True
    assert state.auth_disabled is True
    assert state.consecutive_failures == 0
    assert state.cooldown_until is None
    assert state.last_error == "401 bad key"


def test_success_resets_counter_and_clears_cooldown_and_last_error() -> None:
    state = ProviderHealthState()
    config = HealthConfig(failure_threshold=1)
    state.record_failure(ErrorCategory.TIMEOUT, "timed out", config, 100.0)

    state.record_success()

    assert state.consecutive_failures == 0
    assert state.cooldown_until is None
    assert state.last_error is None


def test_auth_failure_preserves_an_existing_failure_count() -> None:
    """Regression: health writes are get-or-create + mutate, never a replaced state object."""
    state = ProviderHealthState()
    config = HealthConfig(failure_threshold=3)
    state.record_failure(ErrorCategory.TIMEOUT, "timed out", config, 100.0)
    state.record_failure(ErrorCategory.TIMEOUT, "timed out", config, 100.0)

    state.record_failure(ErrorCategory.AUTH, "401 bad key", config, 100.0)

    assert state.consecutive_failures == 2  # not zeroed by the auth bench
    assert state.auth_disabled is True


def test_auth_failure_through_the_router_preserves_an_existing_failure_count() -> None:
    """The same regression, driven through the router's own health writes."""
    script = _Script(
        {"provider_a": [_timeout("provider_a"), _timeout("provider_a"), _http("provider_a", 401)]}
    )
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock)

    _fail(router, times=3)

    report = router.health_report()["provider_a"]
    assert report.consecutive_failures == 2
    assert report.auth_disabled is True


# --- categorization ----------------------------------------------------------


def test_connection_error_categorizes_as_connection() -> None:
    assert categorize_error(_connection("provider_a")) is ErrorCategory.CONNECTION


def test_router_records_connection_error_type_in_metrics() -> None:
    store = _FakeStore()
    script = _Script({"provider_a": [_connection("provider_a")]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, metrics_store=store)

    _fail(router)

    assert [event.error_type for event in store.events] == ["connection"]


# --- filtering ---------------------------------------------------------------


def test_benched_provider_is_excluded_with_trigger_remaining_and_verbatim_error() -> None:
    script = _Script({"provider_a": [_timeout("provider_a", "upstream read timeout")] * 3})
    clock = _FakeClock()
    router = _router(
        [_config("provider_a")],
        script,
        clock=clock,
        health={"failure_cooldown_seconds": 60.0, "failure_threshold": 3},
    )
    _fail(router, times=3)

    clock.advance(12.0)
    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(_calls())

    exclusion = exc_info.value.exclusions[0]
    assert exclusion.reason is FilterReason.IN_COOLDOWN
    assert "48.0s remaining" in exclusion.detail
    assert "after 3 consecutive failures" in exclusion.detail
    assert "upstream read timeout" in exclusion.detail


def test_rate_limited_provider_reports_rate_limiting_as_its_trigger() -> None:
    script = _Script({"provider_a": [_http("provider_a", 429, "quota exceeded")]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock)
    _fail(router)

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(_calls())

    exclusion = exc_info.value.exclusions[0]
    assert exclusion.reason is FilterReason.IN_COOLDOWN
    assert "after rate limiting" in exclusion.detail
    assert "quota exceeded" in exclusion.detail


def test_rate_limit_trigger_is_reported_even_at_the_failure_threshold() -> None:
    """The stored trigger, not the failure count, decides what the detail claims.

    A provider already at the threshold that then gets a 429 was benched by the
    429; inferring the trigger from the count alone would misreport it.
    """
    script = _Script(
        {"provider_a": [_timeout("provider_a")] * 3 + [_http("provider_a", 429, "quota exceeded")]}
    )
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, health=HealthConfig())
    _fail(router, times=3)  # benched by 3 consecutive failures

    clock.advance(60.0)  # cooldown lapses; the probe is rate limited
    _fail(router)

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(_calls())

    detail = exc_info.value.exclusions[0].detail
    assert "after rate limiting" in detail
    assert "consecutive failures" not in detail


def test_auth_bench_exclusion_detail_carries_the_verbatim_error() -> None:
    script = _Script({"provider_a": [_http("provider_a", 401, "invalid api key supplied")]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock)
    _fail(router)

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(_calls())

    exclusion = exc_info.value.exclusions[0]
    assert exclusion.reason is FilterReason.AUTH_DISABLED_THIS_RUN
    assert "invalid api key supplied" in exclusion.detail


def test_cooldown_expiry_makes_provider_eligible_and_next_failure_rebenches_immediately() -> None:
    """Probe-per-window: expiry does not reset the count, so one probe re-benches."""
    script = _Script({"provider_a": [_timeout("provider_a")] * 4})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, health=HealthConfig())
    _fail(router, times=3)

    clock.advance(60.0)
    _fail(router)  # the provider was eligible again and was actually probed

    assert script.invoked == ["provider_a"] * 4
    report = router.health_report()["provider_a"]
    assert report.consecutive_failures == 4  # expiry did not reset the count
    assert report.cooldown_remaining_seconds == 60.0  # re-benched by the single probe


def test_all_providers_benched_fails_fast_with_no_adapter_calls_and_each_root_cause() -> None:
    script = _Script(
        {
            "provider_a": [_timeout("provider_a", "read timeout on provider_a")],
            "provider_b": [_http("provider_b", 429, "provider_b quota exceeded")],
        }
    )
    clock = _FakeClock()
    router = _router(
        [_config("provider_a"), _config("provider_b")],
        script,
        clock=clock,
        health={"failure_threshold": 1},
    )
    _fail(router)
    script.adapters_built.clear()
    script.invoked.clear()

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(_calls())

    assert script.adapters_built == []  # nothing was even constructed
    assert script.invoked == []  # zero network calls
    message = str(exc_info.value)
    assert "read timeout on provider_a" in message
    assert "provider_b quota exceeded" in message


# --- bench logging -----------------------------------------------------------


def test_first_bench_warns_once_with_the_verbatim_error(caplog: pytest.LogCaptureFixture) -> None:
    script = _Script({"provider_a": [_timeout("provider_a", "upstream read timeout")]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, health={"failure_threshold": 1})

    with caplog.at_level(logging.DEBUG, logger="llm_provider_router.router"):
        _fail(router)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "provider_a" in warnings[0].message
    assert "upstream read timeout" in warnings[0].message
    assert "60.0s" in warnings[0].message


def test_repeat_bench_within_one_episode_logs_debug_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = _Script({"provider_a": [_timeout("provider_a")] * 2})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, health={"failure_threshold": 1})

    with caplog.at_level(logging.DEBUG, logger="llm_provider_router.router"):
        _fail(router)  # first bench: warns
        clock.advance(60.0)
        _fail(router)  # probe fails, re-benched with no success in between

    bench_records = [record for record in caplog.records if "Benched" in record.message]
    assert [record.levelno for record in bench_records] == [logging.WARNING, logging.DEBUG]


def test_first_success_after_a_bench_logs_one_recovery_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = _Script({"provider_a": [_timeout("provider_a"), None, None]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, health={"failure_threshold": 1})
    _fail(router)
    clock.advance(60.0)

    with caplog.at_level(logging.INFO, logger="llm_provider_router.router"):
        router.invoke(_calls())  # recovers
        router.invoke(_calls())  # already healthy; must not log again

    recoveries = [record for record in caplog.records if "recovered" in record.message]
    assert len(recoveries) == 1
    assert recoveries[0].levelno == logging.INFO
    assert "provider_a" in recoveries[0].message


def test_a_bench_after_a_recovery_warns_again(caplog: pytest.LogCaptureFixture) -> None:
    """A separate outage is new information, so recovery re-arms the warning."""
    script = _Script({"provider_a": [_timeout("provider_a"), None, _timeout("provider_a")]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, health={"failure_threshold": 1})

    with caplog.at_level(logging.DEBUG, logger="llm_provider_router.router"):
        _fail(router)  # bench: warns
        clock.advance(60.0)
        router.invoke(_calls())  # recovers, ending the episode
        _fail(router)  # a new, separate outage

    bench_records = [record for record in caplog.records if "Benched" in record.message]
    assert [record.levelno for record in bench_records] == [logging.WARNING, logging.WARNING]


# --- health_report -----------------------------------------------------------


def test_health_report_has_an_entry_per_configured_provider_and_healthy_ones_read_clean() -> None:
    script = _Script({})
    clock = _FakeClock()
    router = _router(
        [_config("provider_a"), _config("provider_b", enabled=False)], script, clock=clock
    )

    report = router.health_report()

    assert set(report) == {"provider_a", "provider_b"}
    assert report["provider_a"] == ProviderHealthReport(
        provider_id="provider_a",
        provider_name="provider_a",
        auth_disabled=False,
        consecutive_failures=0,
        cooldown_remaining_seconds=None,
        last_error=None,
    )


def test_health_report_counts_down_remaining_seconds_on_the_injected_clock() -> None:
    script = _Script({"provider_a": [_http("provider_a", 429, "quota exceeded")]})
    clock = _FakeClock()
    router = _router(
        [_config("provider_a")], script, clock=clock, health={"rate_limit_cooldown_seconds": 30.0}
    )
    _fail(router)

    assert router.health_report()["provider_a"].cooldown_remaining_seconds == 30.0
    clock.advance(10.0)
    assert router.health_report()["provider_a"].cooldown_remaining_seconds == 20.0
    clock.advance(20.0)
    # An elapsed cooldown reads as "not benched" rather than as a negative number.
    assert router.health_report()["provider_a"].cooldown_remaining_seconds is None


def test_health_report_reports_the_verbatim_last_error() -> None:
    script = _Script({"provider_a": [_connection("provider_a", "connection refused")]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock)

    _fail(router)

    assert router.health_report()["provider_a"].last_error == "connection refused"


def test_mutating_the_health_report_does_not_change_router_state() -> None:
    script = _Script({"provider_a": [_http("provider_a", 429)]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock)
    _fail(router)

    report = router.health_report()
    report.pop("provider_a")
    report["provider_c"] = ProviderHealthReport(
        provider_id="provider_c", provider_name="provider_c"
    )

    fresh = router.health_report()
    assert set(fresh) == {"provider_a"}
    assert fresh["provider_a"].cooldown_remaining_seconds == 60.0


def test_health_report_entries_are_frozen() -> None:
    script = _Script({})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock)

    with pytest.raises(Exception):  # noqa: B017 -- dataclasses raises FrozenInstanceError
        router.health_report()["provider_a"].auth_disabled = True  # type: ignore[misc]


# --- reset_health ------------------------------------------------------------


def test_reset_health_without_a_name_clears_every_provider() -> None:
    script = _Script(
        {"provider_a": [_http("provider_a", 429)], "provider_b": [_http("provider_b", 401)]}
    )
    clock = _FakeClock()
    router = _router([_config("provider_a"), _config("provider_b")], script, clock=clock)
    _fail(router)

    router.reset_health()

    report = router.health_report()
    assert report["provider_a"] == ProviderHealthReport(
        provider_id="provider_a", provider_name="provider_a"
    )
    assert report["provider_b"] == ProviderHealthReport(
        provider_id="provider_b", provider_name="provider_b"
    )


def test_reset_health_with_a_name_clears_only_that_provider_and_restores_eligibility() -> None:
    script = _Script(
        {
            "provider_a": [_http("provider_a", 429, "quota exceeded"), None],
            "provider_b": [_http("provider_b", 429, "quota exceeded")],
        }
    )
    clock = _FakeClock()
    router = _router([_config("provider_a"), _config("provider_b")], script, clock=clock)
    _fail(router)
    script.invoked.clear()

    router.reset_health("provider_a")

    assert router.health_report()["provider_a"] == ProviderHealthReport(
        provider_id="provider_a", provider_name="provider_a"
    )
    assert router.health_report()["provider_b"].cooldown_remaining_seconds == 60.0
    # provider_a is immediately eligible again -- no waiting out the cooldown.
    assert router.invoke(_calls()) == "provider_a"
    assert script.invoked == ["provider_a"]


def test_reset_health_raises_on_an_unknown_name() -> None:
    script = _Script({})
    clock = _FakeClock()
    router = _router([_config("provider_a"), _config("provider_b")], script, clock=clock)

    with pytest.raises(ConfigError) as exc_info:
        router.reset_health("provider_typo")

    message = str(exc_info.value)
    assert "provider_typo" in message
    assert "provider_a" in message and "provider_b" in message


def test_reset_health_leaves_recorded_metrics_untouched() -> None:
    store = _FakeStore()
    script = _Script({"provider_a": [_http("provider_a", 429)]})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, metrics_store=store)
    _fail(router)
    recorded_before = list(store.events)

    router.reset_health()

    assert store.events == recorded_before
    assert len(store.events) == 1


def test_reset_health_rearms_the_bench_warning(caplog: pytest.LogCaptureFixture) -> None:
    script = _Script({"provider_a": [_timeout("provider_a")] * 2})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock, health={"failure_threshold": 1})

    with caplog.at_level(logging.DEBUG, logger="llm_provider_router.router"):
        _fail(router)
        router.reset_health("provider_a")
        _fail(router)

    bench_records = [record for record in caplog.records if "Benched" in record.message]
    assert [record.levelno for record in bench_records] == [logging.WARNING, logging.WARNING]


# --- HealthConfig ------------------------------------------------------------


def test_health_config_defaults_apply_when_nothing_is_passed() -> None:
    config = HealthConfig()

    assert config.rate_limit_cooldown_seconds == 60.0
    assert config.failure_cooldown_seconds == 60.0
    assert config.failure_threshold == 3


def test_router_defaults_to_health_config_when_health_is_not_passed() -> None:
    script = _Script({"provider_a": [_timeout("provider_a")] * 3})
    clock = _FakeClock()
    router = _router([_config("provider_a")], script, clock=clock)

    _fail(router, times=3)  # the default threshold of 3

    assert router.health_report()["provider_a"].cooldown_remaining_seconds == 60.0


def test_router_accepts_an_equivalent_dict_for_health() -> None:
    script = _Script({"provider_a": [_timeout("provider_a")]})
    clock = _FakeClock()
    router = _router(
        [_config("provider_a")],
        script,
        clock=clock,
        health={"failure_threshold": 1, "failure_cooldown_seconds": 15.0},
    )

    _fail(router)

    assert router.health_report()["provider_a"].cooldown_remaining_seconds == 15.0


def test_router_accepts_a_typed_health_config() -> None:
    script = _Script({"provider_a": [_timeout("provider_a")]})
    clock = _FakeClock()
    router = _router(
        [_config("provider_a")],
        script,
        clock=clock,
        health=HealthConfig(failure_threshold=1, failure_cooldown_seconds=15.0),
    )

    _fail(router)

    assert router.health_report()["provider_a"].cooldown_remaining_seconds == 15.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"failure_treshold": 3},  # typo'd key
        {"rate_limit_cooldown_seconds": 0.0},
        {"rate_limit_cooldown_seconds": -1.0},
        {"failure_cooldown_seconds": 0.0},
        {"failure_cooldown_seconds": -1.0},
        {"failure_threshold": 0},
        {"failure_threshold": -1},
    ],
)
def test_invalid_health_overrides_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HealthConfig.model_validate(overrides)


def test_router_rejects_an_invalid_health_dict_at_construction() -> None:
    """A typo must raise at the boundary, not flow past the constructor unnoticed."""
    with pytest.raises(ValidationError):
        ProviderRouter(
            metrics_scope="test",
            providers=[_config("provider_a")],
            metrics_store=None,
            health={"failure_treshold": 3},
        )


# --- same-call semantics -----------------------------------------------------


def test_a_bench_taken_mid_call_applies_from_the_next_call() -> None:
    """The current call keeps its already-ordered list; the bench starts biting next time."""
    script = _Script({"provider_a": [_timeout("provider_a")]})
    clock = _FakeClock()
    router = _router(
        [_config("provider_a"), _config("provider_b")],
        script,
        clock=clock,
        health={"failure_threshold": 1},
    )

    assert router.invoke(_calls()) == "provider_b"
    assert script.invoked == ["provider_a", "provider_b"]  # a's bench did not truncate the loop
    assert router.health_report()["provider_a"].cooldown_remaining_seconds == 60.0

    script.invoked.clear()
    assert router.invoke(_calls()) == "provider_b"
    assert script.invoked == ["provider_b"]  # now benched, provider_a is skipped


def test_stop_category_mid_call_leaves_health_untouched() -> None:
    """A broken call must not be blamed on the provider that happened to be tried first."""
    script = _Script(
        {
            "provider_a": [
                UnsupportedOperationError(
                    "no such op",
                    provider_id="provider_a",
                    provider_name="provider_a",
                    model="model-a",
                )
            ]
        }
    )
    clock = _FakeClock()
    router = _router(
        [_config("provider_a"), _config("provider_b")],
        script,
        clock=clock,
        health={"failure_threshold": 1},
    )

    _fail(router)

    assert router.health_report()["provider_a"] == ProviderHealthReport(
        provider_id="provider_a", provider_name="provider_a"
    )
