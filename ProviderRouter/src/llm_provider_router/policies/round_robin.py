from __future__ import annotations

from llm_provider_router.config import ProviderConfig
from llm_provider_router.policies.base import RoutingContext


class RoundRobinPolicy:
    """Rotate the starting provider across successive calls.

    The counter indexes into whatever is eligible on each call, not into the
    original config list, so it is always a valid index and self-heals if a
    provider becomes eligible again later. No persistence: the rotation exists
    only for the life of this Python process.
    """

    def __init__(self) -> None:
        self._index = 0

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        """Return eligible rotated so a different provider leads each call."""
        if not eligible:
            return []
        i = self._index % len(eligible)
        self._index += 1
        return eligible[i:] + eligible[:i]
