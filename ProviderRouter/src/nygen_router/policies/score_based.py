from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from nygen_router.config import ProviderConfig
from nygen_router.metrics import MetricsEvent
from nygen_router.policies.base import Policy, RoutingContext
from nygen_router.policies.round_robin import RoundRobinPolicy
from nygen_router.scoring import ScoreWeights, calculate_provider_score
from nygen_router.stats import aggregate_stats

logger = logging.getLogger(__name__)

_QUERY_HALF_LIVES = 6


class HistoryScope(StrEnum):
    """Whether scoring reads the router's current scope or every scope."""

    CURRENT = "current"
    ALL = "all"


class ScoreBasedPolicy:
    """Rank eligible providers from recent observations, with stable tie-breaking."""

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
        if half_life_hours is not None and half_life_hours <= 0:
            raise ValueError("half_life_hours must be positive")
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
        """Return providers best-first, degrading to the tie-break order on no history."""
        rotated = self._tie_break_policy.order(list(eligible), context)
        if context.metrics_store is None or not rotated:
            return rotated

        weight_fn: Callable[[MetricsEvent], float] | None = None
        half_life_hours = self._half_life_hours
        if half_life_hours is None:
            since = self._now() - timedelta(hours=self._lookback_hours)
        else:
            since = self._now() - timedelta(hours=_QUERY_HALF_LIVES * half_life_hours)

            def decay_weight(event: MetricsEvent) -> float:
                age_hours = (self._now() - event.timestamp).total_seconds() / 3600.0
                return float(0.5 ** (age_hours / half_life_hours))

            weight_fn = decay_weight

        try:
            events = context.metrics_store.query_recent(
                since=since,
                metrics_scope=(
                    context.metrics_scope if self._history_scope is HistoryScope.CURRENT else None
                ),
            )
        except Exception:
            logger.log(
                logging.DEBUG if self._metrics_warning_emitted else logging.WARNING,
                "Metrics history is unavailable; using the tie-break policy order.",
                exc_info=True,
            )
            self._metrics_warning_emitted = True
            return rotated

        stats = aggregate_stats(
            events,
            rotated,
            context.call_type,
            weight_fn=weight_fn,
        )
        scores = {
            provider.provider_id: calculate_provider_score(
                stats[provider.provider_id],
                self._weights,
                call_type=context.call_type,
            ).total
            for provider in rotated
        }
        return sorted(rotated, key=lambda provider: scores[provider.provider_id], reverse=True)
