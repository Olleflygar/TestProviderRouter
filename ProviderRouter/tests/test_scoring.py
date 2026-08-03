from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from nygen_router import (
    CallType,
    ProviderScore,
    ProviderStats,
    ScoreWeights,
    calculate_provider_score,
)


def test_higher_regular_success_rate_produces_a_higher_total() -> None:
    lower = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=5.0,
        regular_success_rate=0.2,
        regular_avg_latency_ms=500.0,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=5,
        rate_limit_count=0,
        timeout_count=0,
    )
    higher = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=5.0,
        regular_success_rate=0.8,
        regular_avg_latency_ms=500.0,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=5,
        rate_limit_count=0,
        timeout_count=0,
    )
    weights = ScoreWeights(success_weight=1.0, speed_weight=0.0)

    assert (
        calculate_provider_score(higher, weights).total
        > calculate_provider_score(lower, weights).total
    )


def test_lower_regular_latency_produces_a_higher_total() -> None:
    faster = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=10.0,
        regular_success_rate=1.0,
        regular_avg_latency_ms=100.0,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=0,
        rate_limit_count=0,
        timeout_count=0,
    )
    slower = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=10.0,
        regular_success_rate=1.0,
        regular_avg_latency_ms=10_000.0,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=0,
        rate_limit_count=0,
        timeout_count=0,
    )
    weights = ScoreWeights(success_weight=0.0, speed_weight=1.0)

    assert (
        calculate_provider_score(faster, weights).total
        > calculate_provider_score(slower, weights).total
    )


@pytest.mark.parametrize(
    "call_type", [CallType.REGULAR, CallType.STREAMING], ids=["regular", "streaming"]
)
def test_zero_relevant_attempts_score_exactly_at_the_optimistic_start(
    call_type: CallType,
) -> None:
    stats = ProviderStats(
        provider_id="new",
        provider_name="new",
        regular_attempt_count=0.0,
        regular_success_count=0.0,
        regular_success_rate=None,
        regular_avg_latency_ms=None,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=0,
        rate_limit_count=0,
        timeout_count=0,
    )
    weights = ScoreWeights(optimistic_start=0.63)

    score = calculate_provider_score(stats, weights, call_type=call_type)

    assert score.success_quality == 0.63
    assert score.speed_quality == 0.63
    assert score.total == 0.63


def test_thin_history_stays_near_the_prior_while_deep_history_nears_observed_rate() -> None:
    thin = ProviderStats(
        provider_id="thin",
        provider_name="thin",
        regular_attempt_count=1.0,
        regular_success_count=0.0,
        regular_success_rate=0.0,
        regular_avg_latency_ms=None,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=1,
        rate_limit_count=0,
        timeout_count=0,
    )
    deep = ProviderStats(
        provider_id="deep",
        provider_name="deep",
        regular_attempt_count=100.0,
        regular_success_count=0.0,
        regular_success_rate=0.0,
        regular_avg_latency_ms=None,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=100,
        rate_limit_count=0,
        timeout_count=0,
    )
    weights = ScoreWeights(success_weight=1.0, speed_weight=0.0)

    thin_quality = calculate_provider_score(thin, weights).success_quality
    deep_quality = calculate_provider_score(deep, weights).success_quality

    assert abs(thin_quality - weights.optimistic_start) < abs(thin_quality - 0.0)
    assert deep_quality == pytest.approx(0.0, abs=0.04)


def test_zero_speed_weight_removes_all_speed_influence() -> None:
    faster = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=8.0,
        regular_success_rate=0.8,
        regular_avg_latency_ms=1.0,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=2,
        rate_limit_count=0,
        timeout_count=0,
    )
    slower = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=8.0,
        regular_success_rate=0.8,
        regular_avg_latency_ms=1_000_000.0,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=2,
        rate_limit_count=0,
        timeout_count=0,
    )
    weights = ScoreWeights(success_weight=3.0, speed_weight=0.0)

    faster_score = calculate_provider_score(faster, weights)
    slower_score = calculate_provider_score(slower, weights)

    assert faster_score.speed_quality != slower_score.speed_quality
    assert faster_score.total == faster_score.success_quality
    assert faster_score.total == slower_score.total


def test_zero_success_weight_removes_all_success_influence() -> None:
    reliable = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=5.0,
        regular_success_count=5.0,
        regular_success_rate=1.0,
        regular_avg_latency_ms=300.0,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=0,
        rate_limit_count=0,
        timeout_count=0,
    )
    unreliable = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=5.0,
        regular_success_rate=0.5,
        regular_avg_latency_ms=300.0,
        streaming_attempt_count=0.0,
        streaming_success_count=0.0,
        streaming_success_rate=None,
        streaming_avg_ttft_ms=None,
        recent_error_count=5,
        rate_limit_count=0,
        timeout_count=0,
    )
    weights = ScoreWeights(success_weight=0.0, speed_weight=4.0)

    reliable_score = calculate_provider_score(reliable, weights)
    unreliable_score = calculate_provider_score(unreliable, weights)

    assert reliable_score.success_quality != unreliable_score.success_quality
    assert reliable_score.total == reliable_score.speed_quality
    assert reliable_score.total == unreliable_score.total


def test_both_factor_weights_cannot_be_zero() -> None:
    with pytest.raises(ValidationError):
        ScoreWeights(success_weight=0.0, speed_weight=0.0)


def test_diagnostic_error_tallies_do_not_influence_the_score() -> None:
    no_diagnostics = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=5.0,
        regular_success_rate=0.5,
        regular_avg_latency_ms=300.0,
        streaming_attempt_count=10.0,
        streaming_success_count=5.0,
        streaming_success_rate=0.5,
        streaming_avg_ttft_ms=100.0,
        recent_error_count=0,
        rate_limit_count=0,
        timeout_count=0,
    )
    many_diagnostics = ProviderStats(
        provider_id="provider",
        provider_name="provider",
        regular_attempt_count=10.0,
        regular_success_count=5.0,
        regular_success_rate=0.5,
        regular_avg_latency_ms=300.0,
        streaming_attempt_count=10.0,
        streaming_success_count=5.0,
        streaming_success_rate=0.5,
        streaming_avg_ttft_ms=100.0,
        recent_error_count=10_000,
        rate_limit_count=9_000,
        timeout_count=8_000,
    )
    weights = ScoreWeights()

    assert calculate_provider_score(no_diagnostics, weights) == calculate_provider_score(
        many_diagnostics, weights
    )


def test_streaming_switch_reads_only_the_selected_call_type_fields() -> None:
    stats = ProviderStats(
        provider_id="split",
        provider_name="split",
        regular_attempt_count=100.0,
        regular_success_count=100.0,
        regular_success_rate=1.0,
        regular_avg_latency_ms=50.0,
        streaming_attempt_count=100.0,
        streaming_success_count=0.0,
        streaming_success_rate=0.0,
        streaming_avg_ttft_ms=None,
        recent_error_count=100,
        rate_limit_count=0,
        timeout_count=0,
    )
    weights = ScoreWeights()

    regular = calculate_provider_score(stats, weights, call_type=CallType.REGULAR)
    streaming = calculate_provider_score(stats, weights, call_type=CallType.STREAMING)

    assert regular.success_quality == pytest.approx((5 * 0.75 + 100 * 1.0) / 105)
    assert regular.speed_quality == pytest.approx((5 * 0.75 + 100 * (2000 / 2050)) / 105)
    assert streaming.success_quality == pytest.approx((5 * 0.75 + 100 * 0.0) / 105)
    assert streaming.speed_quality == 0.75
    assert regular.total > streaming.total


@pytest.mark.parametrize(
    ("stats", "weights", "call_type"),
    [
        pytest.param(
            ProviderStats(
                provider_id="new",
                provider_name="new",
                regular_attempt_count=0.0,
                regular_success_count=0.0,
                regular_success_rate=None,
                regular_avg_latency_ms=None,
                streaming_attempt_count=0.0,
                streaming_success_count=0.0,
                streaming_success_rate=None,
                streaming_avg_ttft_ms=None,
                recent_error_count=0,
                rate_limit_count=0,
                timeout_count=0,
            ),
            ScoreWeights(),
            CallType.REGULAR,
            id="zero-attempts",
        ),
        pytest.param(
            ProviderStats(
                provider_id="slow",
                provider_name="slow",
                regular_attempt_count=100.0,
                regular_success_count=100.0,
                regular_success_rate=0.5,
                regular_avg_latency_ms=1_000_000_000.0,
                streaming_attempt_count=0.0,
                streaming_success_count=0.0,
                streaming_success_rate=None,
                streaming_avg_ttft_ms=None,
                recent_error_count=50,
                rate_limit_count=0,
                timeout_count=0,
            ),
            ScoreWeights(success_weight=0.0, speed_weight=100.0),
            CallType.REGULAR,
            id="very-high-latency",
        ),
        pytest.param(
            ProviderStats(
                provider_id="excellent",
                provider_name="excellent",
                regular_attempt_count=1_000.0,
                regular_success_count=1_000.0,
                regular_success_rate=1.0,
                regular_avg_latency_ms=0.0,
                streaming_attempt_count=0.0,
                streaming_success_count=0.0,
                streaming_success_rate=None,
                streaming_avg_ttft_ms=None,
                recent_error_count=0,
                rate_limit_count=0,
                timeout_count=0,
            ),
            ScoreWeights(success_weight=10.0, speed_weight=0.1, optimistic_start=1.0),
            CallType.REGULAR,
            id="perfect-low-latency",
        ),
        pytest.param(
            ProviderStats(
                provider_id="stream",
                provider_name="stream",
                regular_attempt_count=10.0,
                regular_success_count=0.0,
                regular_success_rate=0.0,
                regular_avg_latency_ms=None,
                streaming_attempt_count=10.0,
                streaming_success_count=10.0,
                streaming_success_rate=1.0,
                streaming_avg_ttft_ms=1.0,
                recent_error_count=10,
                rate_limit_count=0,
                timeout_count=0,
            ),
            ScoreWeights(success_weight=0.3, speed_weight=7.0, optimistic_start=0.0),
            CallType.STREAMING,
            id="streaming",
        ),
    ],
)
def test_total_is_always_a_quality_between_zero_and_one(
    stats: ProviderStats,
    weights: ScoreWeights,
    call_type: CallType,
) -> None:
    total = calculate_provider_score(stats, weights, call_type=call_type).total

    assert 0 <= total <= 1


def test_score_weights_defaults_are_valid() -> None:
    assert ScoreWeights() == ScoreWeights(
        success_weight=1.0,
        speed_weight=1.0,
        regular_latency_reference_ms=2000.0,
        streaming_ttft_reference_ms=500.0,
        optimistic_start=0.75,
        optimistic_start_pretend_attempts=5.0,
    )


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param({"success_weight": -0.1}, id="negative-success-weight"),
        pytest.param({"speed_weight": -0.1}, id="negative-speed-weight"),
        pytest.param({"regular_latency_reference_ms": 0.0}, id="zero-regular-reference"),
        pytest.param({"streaming_ttft_reference_ms": 0.0}, id="zero-streaming-reference"),
        pytest.param({"optimistic_start": -0.1}, id="optimistic-start-below-zero"),
        pytest.param({"optimistic_start": 1.1}, id="optimistic-start-above-one"),
        pytest.param({"optimistic_start_pretend_attempts": 0.0}, id="zero-pretend-attempts"),
        pytest.param({"optimistic_start_pretend_attempts": -1.0}, id="negative-pretend-attempts"),
    ],
)
def test_score_weights_reject_each_invalid_field_value(invalid: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        ScoreWeights(**invalid)  # type: ignore[arg-type]


def test_score_weights_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ScoreWeights.model_validate({"unknown": 1})


def test_provider_score_is_frozen() -> None:
    score = ProviderScore(
        provider_id="provider",
        provider_name="provider",
        total=0.5,
        success_quality=0.5,
        speed_quality=0.5,
    )

    with pytest.raises(FrozenInstanceError):
        score.total = 0.8  # type: ignore[misc]
