from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from llm_provider_router.stats import ProviderStats
from llm_provider_router.types import CallType


class ScoreWeights(BaseModel):
    """Relative factor weights and evidence priors used by provider scoring."""

    model_config = ConfigDict(extra="forbid")

    success_weight: float = 1.0
    speed_weight: float = 1.0
    regular_latency_reference_ms: float = 2000.0
    streaming_ttft_reference_ms: float = 500.0
    optimistic_start: float = 0.75
    optimistic_start_pretend_attempts: float = 5.0

    @field_validator("success_weight", "speed_weight")
    @classmethod
    def _weights_must_be_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("must be non-negative")
        return value

    @field_validator("regular_latency_reference_ms", "streaming_ttft_reference_ms")
    @classmethod
    def _references_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator("optimistic_start")
    @classmethod
    def _optimistic_start_must_be_a_quality(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("must be between 0 and 1")
        return value

    @field_validator("optimistic_start_pretend_attempts")
    @classmethod
    def _pretend_attempts_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @model_validator(mode="after")
    def _at_least_one_factor_must_be_weighted(self) -> Self:
        if self.success_weight == 0 and self.speed_weight == 0:
            raise ValueError("success_weight and speed_weight cannot both be zero")
        return self


@dataclass(frozen=True)
class ProviderScore:
    """One provider's comparable total with its explainable components."""

    provider_id: str
    provider_name: str
    total: float
    success_quality: float
    speed_quality: float


def calculate_provider_score(
    stats: ProviderStats,
    weights: ScoreWeights,
    *,
    call_type: CallType = CallType.REGULAR,
) -> ProviderScore:
    """Turn one provider's operation-specific observations into a pure score."""
    if call_type is CallType.STREAMING:
        attempt_count = stats.streaming_attempt_count
        success_count = stats.streaming_success_count
        success_rate = stats.streaming_success_rate
        average_latency_ms = stats.streaming_avg_ttft_ms
        reference_ms = weights.streaming_ttft_reference_ms
    else:
        attempt_count = stats.regular_attempt_count
        success_count = stats.regular_success_count
        success_rate = stats.regular_success_rate
        average_latency_ms = stats.regular_avg_latency_ms
        reference_ms = weights.regular_latency_reference_ms

    pretend = weights.optimistic_start_pretend_attempts
    observed_success = 0.0 if success_rate is None else success_rate
    success_quality = (pretend * weights.optimistic_start + attempt_count * observed_success) / (
        pretend + attempt_count
    )

    raw_speed_quality = (
        0.0 if average_latency_ms is None else reference_ms / (reference_ms + average_latency_ms)
    )
    speed_quality = (pretend * weights.optimistic_start + success_count * raw_speed_quality) / (
        pretend + success_count
    )

    total = (weights.success_weight * success_quality + weights.speed_weight * speed_quality) / (
        weights.success_weight + weights.speed_weight
    )

    return ProviderScore(
        provider_id=stats.provider_id,
        provider_name=stats.provider_name,
        total=total,
        success_quality=success_quality,
        speed_quality=speed_quality,
    )
