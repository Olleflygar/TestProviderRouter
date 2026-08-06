from __future__ import annotations

from collections.abc import Iterable

from nygen_router import (
    ExponentialScoreWeighting,
    MetricsEvent,
    ScoreAggregate,
    ScoreAggregateQuery,
)
from nygen_router.errors import ErrorCategory


def aggregate_events_for_score_query(
    events: Iterable[MetricsEvent],
    query: ScoreAggregateQuery,
) -> list[ScoreAggregate]:
    """Reference in-memory implementation of the public score aggregate contract."""
    event_list = list(events)
    results: list[ScoreAggregate] = []
    for provider in query.providers:
        attempt_weight = 0.0
        success_weight = 0.0
        successful_latency_weight = 0.0
        successful_latency_total_ms = 0.0
        recent_error_count = 0
        rate_limit_count = 0
        timeout_count = 0
        for event in event_list:
            if (
                event.timestamp < query.since
                or (query.metrics_scope is not None and event.metrics_scope != query.metrics_scope)
                or event.provider_id != provider.provider_id
                or event.model != provider.model
                or event.protocol is not provider.protocol
                or event.call_type is not query.call_type
            ):
                continue
            weight = _event_weight(event, query)
            attempt_weight += weight
            if event.success:
                success_weight += weight
                if event.latency_ms is not None:
                    successful_latency_weight += weight
                    successful_latency_total_ms += weight * event.latency_ms
            else:
                recent_error_count += 1
                rate_limit_count += event.error_type == ErrorCategory.RATE_LIMIT.value
                timeout_count += event.error_type == ErrorCategory.TIMEOUT.value
        results.append(
            ScoreAggregate(
                provider_id=provider.provider_id,
                attempt_weight=attempt_weight,
                success_weight=success_weight,
                successful_latency_weight=successful_latency_weight,
                successful_latency_total_ms=successful_latency_total_ms,
                recent_error_count=recent_error_count,
                rate_limit_count=rate_limit_count,
                timeout_count=timeout_count,
            )
        )
    return results


def zero_score_aggregates(query: ScoreAggregateQuery) -> list[ScoreAggregate]:
    return aggregate_events_for_score_query((), query)


def _event_weight(event: MetricsEvent, query: ScoreAggregateQuery) -> float:
    weighting = query.weighting
    if not isinstance(weighting, ExponentialScoreWeighting):
        return 1.0
    age_hours = (query.reference_time - event.timestamp).total_seconds() / 3600.0
    return float(0.5 ** (age_hours / weighting.half_life_hours))
