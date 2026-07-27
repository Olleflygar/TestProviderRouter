from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from nygen_router.config import ProviderConfig
from nygen_router.policies.base import Policy, RoutingContext
from nygen_router.policies.round_robin import RoundRobinPolicy
from nygen_router.scoring import ScoreWeights, calculate_provider_score
from nygen_router.stats import aggregate_stats

logger = logging.getLogger(__name__)


class ScoreBasedPolicy:
    """Rank eligible providers from recent observations, with stable tie-breaking."""

    def __init__(
        self,
        *,
        weights: ScoreWeights | None = None,
        lookback_hours: float = 336.0,
        use_streaming: bool = False,
        tie_break_policy: Policy | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if lookback_hours <= 0:
            raise ValueError("lookback_hours must be positive")
        self._weights = ScoreWeights() if weights is None else weights
        self._lookback_hours = lookback_hours
        self._use_streaming = use_streaming
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

        since = self._now() - timedelta(hours=self._lookback_hours)
        try:
            events = context.metrics_store.query_recent(since=since)
        except Exception:
            logger.log(
                logging.DEBUG if self._metrics_warning_emitted else logging.WARNING,
                "Metrics history is unavailable; using the tie-break policy order.",
                exc_info=True,
            )
            self._metrics_warning_emitted = True
            return rotated

        stats = aggregate_stats(events, [provider.name for provider in rotated])
        scores = {
            provider.name: calculate_provider_score(
                stats[provider.name],
                self._weights,
                use_streaming=self._use_streaming,
            ).total
            for provider in rotated
        }
        return sorted(rotated, key=lambda provider: scores[provider.name], reverse=True)
