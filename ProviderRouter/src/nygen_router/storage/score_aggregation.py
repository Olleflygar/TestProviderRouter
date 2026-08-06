from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from numbers import Real

from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.stats import ProviderStats
from nygen_router.types import CallType

_CONSISTENCY_REL_TOLERANCE = 1e-12
_CONSISTENCY_ABS_TOLERANCE = 1e-12


@dataclass(frozen=True)
class ScoreAggregateProvider:
    """One current provider partition requested by score-based routing."""

    provider_id: str
    model: str
    protocol: ApiProtocol

    def __post_init__(self) -> None:
        provider_id = _nonblank_string(self.provider_id, field_name="provider_id")
        model = _nonblank_string(self.model, field_name="model")
        if not isinstance(self.protocol, ApiProtocol):
            raise TypeError("protocol must be an ApiProtocol value")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "model", model)


@dataclass(frozen=True)
class FlatScoreWeighting:
    """Count every matching event with weight 1.0."""


@dataclass(frozen=True)
class ExponentialScoreWeighting:
    """Exponentially decay evidence by age from one captured reference time."""

    half_life_hours: float

    def __post_init__(self) -> None:
        value = _finite_real(self.half_life_hours, field_name="half_life_hours")
        if value <= 0:
            raise ValueError("half_life_hours must be positive")
        object.__setattr__(self, "half_life_hours", value)


@dataclass(frozen=True)
class ScoreAggregateQuery:
    """One-reference-time request for exact scope/partition/call-type totals."""

    providers: tuple[ScoreAggregateProvider, ...]
    metrics_scope: str | None
    call_type: CallType
    since: datetime
    reference_time: datetime
    weighting: FlatScoreWeighting | ExponentialScoreWeighting

    def __post_init__(self) -> None:
        try:
            providers = tuple(self.providers)
        except TypeError as exc:
            raise TypeError(
                "providers must be an iterable of ScoreAggregateProvider values"
            ) from exc
        if not providers:
            raise ValueError("providers must not be empty")
        if any(not isinstance(provider, ScoreAggregateProvider) for provider in providers):
            raise TypeError("providers must contain only ScoreAggregateProvider values")
        provider_ids = [provider.provider_id for provider in providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("providers must be distinct by provider_id")

        metrics_scope = self.metrics_scope
        if metrics_scope is not None:
            metrics_scope = _nonblank_string(metrics_scope, field_name="metrics_scope")
        if not isinstance(self.call_type, CallType):
            raise TypeError("call_type must be a CallType value")
        since = _aware_utc(self.since, field_name="since")
        reference_time = _aware_utc(self.reference_time, field_name="reference_time")
        if since > reference_time:
            raise ValueError("since must not be later than reference_time")
        if not isinstance(self.weighting, (FlatScoreWeighting, ExponentialScoreWeighting)):
            raise TypeError("weighting must be FlatScoreWeighting or ExponentialScoreWeighting")

        object.__setattr__(self, "providers", providers)
        object.__setattr__(self, "metrics_scope", metrics_scope)
        object.__setattr__(self, "since", since)
        object.__setattr__(self, "reference_time", reference_time)


@dataclass(frozen=True)
class ScoreAggregate:
    """Intermediate totals; explicit zeros mean empty history, absence is invalid."""

    provider_id: str
    attempt_weight: float
    success_weight: float
    successful_latency_weight: float
    successful_latency_total_ms: float
    recent_error_count: int
    rate_limit_count: int
    timeout_count: int


def validate_score_aggregates(
    query: ScoreAggregateQuery,
    aggregates: Iterable[ScoreAggregate],
) -> dict[str, ScoreAggregate]:
    """Validate one complete aggregate response and map it by canonical provider ID."""
    if not isinstance(query, ScoreAggregateQuery):
        raise TypeError("query must be a ScoreAggregateQuery")
    try:
        rows = tuple(aggregates)
    except TypeError as exc:
        raise TypeError("score aggregate results must be iterable") from exc

    expected_ids = tuple(provider.provider_id for provider in query.providers)
    expected_set = set(expected_ids)
    if len(rows) != len(expected_ids):
        raise ValueError(
            f"score aggregate result returned {len(rows)} rows; expected {len(expected_ids)}"
        )

    by_id: dict[str, ScoreAggregate] = {}
    for row in rows:
        if not isinstance(row, ScoreAggregate):
            raise TypeError("score aggregate results must contain only ScoreAggregate values")
        if row.provider_id not in expected_set:
            raise ValueError(f"unexpected score aggregate provider_id {row.provider_id!r}")
        if row.provider_id in by_id:
            raise ValueError(f"duplicate score aggregate provider_id {row.provider_id!r}")

        attempt_weight = _nonnegative_finite_real(row.attempt_weight, field_name="attempt_weight")
        success_weight = _nonnegative_finite_real(row.success_weight, field_name="success_weight")
        latency_weight = _nonnegative_finite_real(
            row.successful_latency_weight,
            field_name="successful_latency_weight",
        )
        _nonnegative_finite_real(
            row.successful_latency_total_ms,
            field_name="successful_latency_total_ms",
        )
        if _meaningfully_greater(success_weight, attempt_weight):
            raise ValueError("success_weight must not exceed attempt_weight")
        if _meaningfully_greater(latency_weight, success_weight):
            raise ValueError("successful_latency_weight must not exceed success_weight")
        _nonnegative_integer(row.recent_error_count, field_name="recent_error_count")
        _nonnegative_integer(row.rate_limit_count, field_name="rate_limit_count")
        _nonnegative_integer(row.timeout_count, field_name="timeout_count")
        by_id[row.provider_id] = row

    missing = expected_set.difference(by_id)
    if missing:
        raise ValueError(f"missing score aggregate provider_id {sorted(missing)[0]!r}")
    return by_id


def provider_stats_from_score_aggregate(
    aggregate: ScoreAggregate,
    provider: ProviderConfig,
    call_type: CallType,
) -> ProviderStats:
    """Derive rates/averages and one stats bucket from validated intermediate totals."""
    if aggregate.provider_id != provider.provider_id:
        raise ValueError("aggregate provider_id does not match the current provider configuration")
    if not isinstance(call_type, CallType):
        raise TypeError("call_type must be a CallType value")
    success_rate = (
        None
        if aggregate.attempt_weight == 0
        else aggregate.success_weight / aggregate.attempt_weight
    )
    average_latency = (
        None
        if aggregate.successful_latency_weight == 0
        else aggregate.successful_latency_total_ms / aggregate.successful_latency_weight
    )
    if call_type is CallType.REGULAR:
        regular_attempts = aggregate.attempt_weight
        regular_successes = aggregate.success_weight
        regular_success_rate = success_rate
        regular_average_latency = average_latency
        streaming_attempts = 0.0
        streaming_successes = 0.0
        streaming_success_rate = None
        streaming_average_latency = None
    else:
        regular_attempts = 0.0
        regular_successes = 0.0
        regular_success_rate = None
        regular_average_latency = None
        streaming_attempts = aggregate.attempt_weight
        streaming_successes = aggregate.success_weight
        streaming_success_rate = success_rate
        streaming_average_latency = average_latency
    return ProviderStats(
        provider_id=provider.provider_id,
        provider_name=provider.name,
        regular_attempt_count=regular_attempts,
        regular_success_count=regular_successes,
        regular_success_rate=regular_success_rate,
        regular_avg_latency_ms=regular_average_latency,
        streaming_attempt_count=streaming_attempts,
        streaming_success_count=streaming_successes,
        streaming_success_rate=streaming_success_rate,
        streaming_avg_ttft_ms=streaming_average_latency,
        recent_error_count=aggregate.recent_error_count,
        rate_limit_count=aggregate.rate_limit_count,
        timeout_count=aggregate.timeout_count,
    )


def _nonblank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _aware_utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _finite_real(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{field_name} must be a real number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _nonnegative_finite_real(value: object, *, field_name: str) -> float:
    normalized = _finite_real(value, field_name=field_name)
    if normalized < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return normalized


def _nonnegative_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an exact integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _meaningfully_greater(left: float, right: float) -> bool:
    return left > right and not math.isclose(
        left,
        right,
        rel_tol=_CONSISTENCY_REL_TOLERANCE,
        abs_tol=_CONSISTENCY_ABS_TOLERANCE,
    )
