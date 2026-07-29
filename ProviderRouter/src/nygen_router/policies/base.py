from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nygen_router.config import ProviderConfig
from nygen_router.storage.base import MetricsStore


@dataclass(frozen=True)
class RoutingContext:
    """Per-call runtime data made available to routing policies."""

    metrics_store: MetricsStore | None


class Policy(Protocol):
    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        """Return eligible providers in the order to attempt them this call."""
        ...
