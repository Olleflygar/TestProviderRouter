from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nygen_router.config import ProviderConfig
from nygen_router.storage.base import MetricsStore
from nygen_router.types import CallType


@dataclass(frozen=True)
class RoutingContext:
    """Per-call runtime data made available to routing policies."""

    metrics_store: MetricsStore | None
    metrics_scope: str
    call_type: CallType


class Policy(Protocol):
    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        """Return eligible providers in the order to attempt them this call."""
        ...
