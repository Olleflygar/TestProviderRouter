from __future__ import annotations

from typing import Protocol

from nygen_router.config import ProviderConfig


class Policy(Protocol):
    def order(self, eligible: list[ProviderConfig]) -> list[ProviderConfig]:
        """Return eligible providers in the order to attempt them this call."""
        ...
