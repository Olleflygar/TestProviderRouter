from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from nygen_router.config import ProviderConfig
from nygen_router.policies.base import Policy, RoutingContext
from nygen_router.policies.round_robin import RoundRobinPolicy
from nygen_router.scoring import ScoreWeights, calculate_provider_score
from nygen_router.storage.score_aggregation import (
    ExponentialScoreWeighting,
    FlatScoreWeighting,
    ScoreAggregateProvider,
    ScoreAggregateQuery,
    provider_stats_from_score_aggregate,
    validate_score_aggregates,
)

logger = logging.getLogger(__name__)

_QUERY_HALF_LIVES = 6


class HistoryScope(StrEnum):
    """Whether scoring reads the router's current scope or every scope."""

    CURRENT = "current"
    ALL = "all"


class ScoreBasedPolicy:
    """Rank providers through one bounded store aggregate, with stable tie-breaking."""

    def __init__(
        self,
        *,
        weights: ScoreWeights | None = None,
        lookback_hours: float = 336.0,
        half_life_hours: float | None = None,
        history_scope: HistoryScope = HistoryScope.CURRENT,
        tie_break_policy: Policy | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if lookback_hours <= 0:
            raise ValueError("lookback_hours must be positive")
        if not math.isfinite(lookback_hours):
            raise ValueError("lookback_hours must be finite")
        if half_life_hours is not None and half_life_hours <= 0:
            raise ValueError("half_life_hours must be positive")
        if half_life_hours is not None and not math.isfinite(half_life_hours):
            raise ValueError("half_life_hours must be finite")
        self._weights = ScoreWeights() if weights is None else weights
        self._lookback_hours = lookback_hours
        self._half_life_hours = half_life_hours
        self._history_scope = HistoryScope(history_scope)
        self._tie_break_policy = (
            RoundRobinPolicy() if tie_break_policy is None else tie_break_policy
        )
        self._now = now
        self._metrics_warning_emitted = False

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        """Return providers best-first; aggregate failure/invalidity preserves the baseline."""
        rotated = self._tie_break_policy.order(list(eligible), context)
        if context.metrics_store is None or not rotated:
            return rotated

        reference_time = self._now()
        half_life_hours = self._half_life_hours
        weighting: FlatScoreWeighting | ExponentialScoreWeighting
        if half_life_hours is None:
            since = reference_time - timedelta(hours=self._lookback_hours)
            weighting = FlatScoreWeighting()
        else:
            since = reference_time - timedelta(hours=_QUERY_HALF_LIVES * half_life_hours)
            weighting = ExponentialScoreWeighting(half_life_hours=half_life_hours)

        requested: list[ScoreAggregateProvider] = []
        providers_by_id: dict[str, ProviderConfig] = {}
        for provider in rotated:
            if provider.provider_id in providers_by_id:
                continue
            providers_by_id[provider.provider_id] = provider
            requested.append(
                ScoreAggregateProvider(
                    provider_id=provider.provider_id,
                    model=provider.model,
                    protocol=provider.protocol,
                )
            )

        try:
            query = ScoreAggregateQuery(
                providers=tuple(requested),
                metrics_scope=(
                    context.metrics_scope if self._history_scope is HistoryScope.CURRENT else None
                ),
                call_type=context.call_type,
                since=since,
                reference_time=reference_time,
                weighting=weighting,
            )
            aggregates = context.metrics_store.query_score_aggregates(query)
            aggregates_by_id = validate_score_aggregates(query, aggregates)
            scores = {
                provider_id: calculate_provider_score(
                    provider_stats_from_score_aggregate(
                        aggregates_by_id[provider_id],
                        provider,
                        context.call_type,
                    ),
                    self._weights,
                    call_type=context.call_type,
                ).total
                for provider_id, provider in providers_by_id.items()
            }
        except Exception:
            logger.log(
                logging.DEBUG if self._metrics_warning_emitted else logging.WARNING,
                "Metrics history is unavailable; using the tie-break policy order.",
                exc_info=True,
            )
            self._metrics_warning_emitted = True
            return rotated

        return sorted(rotated, key=lambda provider: scores[provider.provider_id], reverse=True)
