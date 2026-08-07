from __future__ import annotations

import pytest

from llm_provider_router import ApiProtocol, CallType, MetricsEvent, ProviderConfig, aggregate_stats
from llm_provider_router.errors import ErrorCategory


def _event(
    provider_name: str = "provider_a",
    *,
    success: bool = True,
    latency_ms: float | None = 100.0,
    error_type: str | None = None,
    stream: bool = False,
) -> MetricsEvent:
    return MetricsEvent(
        provider_id=provider_name,
        metrics_scope="test",
        call_type=CallType.STREAMING if stream else CallType.REGULAR,
        provider_name=provider_name,
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        success=success,
        latency_ms=latency_ms,
        error_type=error_type,
    )


def _provider(
    provider_id: str = "provider_a",
    *,
    name: str | None = None,
    model: str = "model-a",
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=provider_id if name is None else name,
        protocol=protocol,
        model=model,
        base_url="https://provider.example.com/v1",
        api_key="secret",
    )


def _failure(provider_name: str = "provider_a", **kwargs: object) -> MetricsEvent:
    """A failed attempt, defaulting to the category that carries no special tally."""
    kwargs.setdefault("error_type", ErrorCategory.SERVER_ERROR.value)
    return _event(provider_name, success=False, **kwargs)  # type: ignore[arg-type]


def test_a_provider_with_only_regular_events_leaves_its_streaming_fields_empty() -> None:
    events = [_event(latency_ms=100.0), _failure(latency_ms=10.0)]

    stats = aggregate_stats(events, [_provider()], CallType.REGULAR)["provider_a"]

    assert stats.provider_name == "provider_a"
    assert stats.regular_attempt_count == 2.0
    assert stats.regular_success_count == 1.0
    assert stats.regular_success_rate == pytest.approx(0.5)
    assert stats.regular_avg_latency_ms == pytest.approx(100.0)
    assert stats.streaming_attempt_count == 0.0
    assert stats.streaming_success_count == 0.0
    assert stats.streaming_success_rate is None
    assert stats.streaming_avg_ttft_ms is None


def test_a_provider_with_only_streaming_events_leaves_its_regular_fields_empty() -> None:
    events = [_event(stream=True, latency_ms=40.0), _failure(stream=True, latency_ms=5.0)]

    stats = aggregate_stats(events, [_provider()], CallType.STREAMING)["provider_a"]

    assert stats.streaming_attempt_count == 2.0
    assert stats.streaming_success_count == 1.0
    assert stats.streaming_success_rate == pytest.approx(0.5)
    assert stats.streaming_avg_ttft_ms == pytest.approx(40.0)
    assert stats.regular_attempt_count == 0.0
    assert stats.regular_success_count == 0.0
    assert stats.regular_success_rate is None
    assert stats.regular_avg_latency_ms is None


def test_success_rate_and_average_latency_over_a_mix_of_outcomes() -> None:
    events = [
        _event(latency_ms=100.0),
        _event(latency_ms=200.0),
        _event(latency_ms=300.0),
        _failure(latency_ms=50.0),
    ]

    stats = aggregate_stats(events, [_provider()], CallType.REGULAR)["provider_a"]

    assert stats.regular_attempt_count == 4.0
    assert stats.regular_success_count == 3.0
    assert stats.regular_success_rate == pytest.approx(0.75)
    assert stats.regular_avg_latency_ms == pytest.approx(200.0)


def test_average_latency_ignores_a_fast_failure() -> None:
    """A provider that fails in 5ms must not come out looking faster than one that works."""
    events = [_failure(latency_ms=5.0), _event(latency_ms=500.0)]

    stats = aggregate_stats(events, [_provider()], CallType.REGULAR)["provider_a"]

    assert stats.regular_avg_latency_ms == pytest.approx(500.0)


def test_average_ttft_ignores_a_stream_that_died_before_its_first_chunk() -> None:
    events = [_failure(stream=True, latency_ms=None), _event(stream=True, latency_ms=800.0)]

    stats = aggregate_stats(events, [_provider()], CallType.STREAMING)["provider_a"]

    assert stats.streaming_attempt_count == 2.0
    assert stats.streaming_success_count == 1.0
    assert stats.streaming_avg_ttft_ms == pytest.approx(800.0)


def test_a_completed_stream_with_no_chunk_to_time_reports_no_average() -> None:
    """No time-to-first-chunk is reported as no evidence, never as an instant 0.0."""
    stats = aggregate_stats(
        [_event(stream=True, latency_ms=None)], [_provider()], CallType.STREAMING
    )["provider_a"]

    assert stats.streaming_attempt_count == 1.0
    assert stats.streaming_success_count == 1.0
    assert stats.streaming_success_rate == pytest.approx(1.0)
    assert stats.streaming_avg_ttft_ms is None


def test_a_provider_with_no_events_still_gets_an_entry() -> None:
    """PR 8's optimistic start needs a real entry to fall back on, not a missing key."""
    result = aggregate_stats(
        [_event("provider_a")], [_provider(), _provider("brand_new")], CallType.REGULAR
    )

    assert set(result) == {"provider_a", "brand_new"}
    stats = result["brand_new"]
    assert stats.provider_name == "brand_new"
    assert stats.regular_attempt_count == 0.0
    assert stats.regular_success_count == 0.0
    assert stats.streaming_attempt_count == 0.0
    assert stats.streaming_success_count == 0.0
    assert stats.regular_success_rate is None
    assert stats.regular_avg_latency_ms is None
    assert stats.streaming_success_rate is None
    assert stats.streaming_avg_ttft_ms is None
    assert stats.recent_error_count == 0
    assert stats.rate_limit_count == 0
    assert stats.timeout_count == 0


def test_events_for_a_provider_not_asked_about_are_ignored() -> None:
    events = [_event("provider_a"), _event("retired_provider"), _failure("retired_provider")]

    result = aggregate_stats(events, [_provider()], CallType.REGULAR)

    assert set(result) == {"provider_a"}
    assert result["provider_a"].regular_attempt_count == 1.0
    assert result["provider_a"].recent_error_count == 0


def test_error_tallies_count_each_category_separately() -> None:
    events = [
        _event(),
        _failure(error_type=ErrorCategory.RATE_LIMIT.value),
        _failure(error_type=ErrorCategory.RATE_LIMIT.value),
        _failure(error_type=ErrorCategory.TIMEOUT.value),
        _failure(error_type=ErrorCategory.SERVER_ERROR.value),
        _failure(error_type=ErrorCategory.STREAM_INTERRUPTED.value),
    ]

    stats = aggregate_stats(events, [_provider()], CallType.REGULAR)["provider_a"]

    assert stats.recent_error_count == 5  # every failure, whatever its category
    assert stats.rate_limit_count == 2
    assert stats.timeout_count == 1


def test_regular_and_streaming_events_never_influence_each_other() -> None:
    events = [
        _event(latency_ms=100.0),
        _event(latency_ms=300.0),
        _failure(stream=True, latency_ms=20.0),
        _failure(stream=True, latency_ms=20.0),
    ]

    stats = aggregate_stats(events, [_provider()], CallType.REGULAR)["provider_a"]

    # Two perfect regular calls stay perfect despite two dead streams.
    assert stats.regular_success_rate == pytest.approx(1.0)
    assert stats.regular_avg_latency_ms == pytest.approx(200.0)
    assert stats.streaming_success_rate is None
    assert stats.streaming_avg_ttft_ms is None


def test_weight_fn_scales_every_count_it_touches() -> None:
    events = [_event(latency_ms=100.0), _failure(latency_ms=10.0)]

    unweighted = aggregate_stats(events, [_provider()], CallType.REGULAR)["provider_a"]
    weighted = aggregate_stats(
        events, [_provider()], CallType.REGULAR, weight_fn=lambda event: 2.0
    )["provider_a"]

    assert weighted.regular_attempt_count == pytest.approx(unweighted.regular_attempt_count * 2)
    assert weighted.regular_success_count == pytest.approx(unweighted.regular_success_count * 2)
    # The exact tallies are not weighted: they are diagnostics, not evidence.
    assert weighted.recent_error_count == unweighted.recent_error_count


def test_weight_fn_reaches_the_rates_and_averages_too() -> None:
    """A per-event weight has to move every derived figure, not just the counts."""
    events = [_event(latency_ms=100.0), _event(latency_ms=300.0), _failure(latency_ms=10.0)]

    unweighted = aggregate_stats(events, [_provider()], CallType.REGULAR)["provider_a"]
    weighted = aggregate_stats(
        events,
        [_provider()],
        CallType.REGULAR,
        weight_fn=lambda event: 3.0 if event.latency_ms == 300.0 else 1.0,
    )["provider_a"]

    assert unweighted.regular_attempt_count == pytest.approx(3.0)
    assert unweighted.regular_success_rate == pytest.approx(2 / 3)
    assert unweighted.regular_avg_latency_ms == pytest.approx(200.0)

    # The 300ms success now counts three times: one more attempt-weight than
    # the other two combined, and it pulls both the rate and the average.
    assert weighted.regular_attempt_count == pytest.approx(5.0)
    assert weighted.regular_success_count == pytest.approx(4.0)
    assert weighted.regular_success_rate == pytest.approx(0.8)
    assert weighted.regular_avg_latency_ms == pytest.approx(250.0)


def test_weight_fn_receives_each_event_exactly_once() -> None:
    events = [_event(), _failure(), _event(stream=True)]
    seen: list[MetricsEvent] = []

    def weight(event: MetricsEvent) -> float:
        seen.append(event)
        return 1.0

    aggregate_stats(events, [_provider()], CallType.REGULAR, weight_fn=weight)

    assert seen == events[:2]


def test_every_requested_provider_is_reported_once_in_the_order_asked_for() -> None:
    events = [_event("provider_b"), _event("provider_a")]

    result = aggregate_stats(
        events,
        [_provider("provider_a"), _provider("provider_b"), _provider("provider_c")],
        CallType.REGULAR,
    )

    assert list(result) == ["provider_a", "provider_b", "provider_c"]
    assert all(name == stats.provider_name for name, stats in result.items())
