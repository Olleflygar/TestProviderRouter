from __future__ import annotations

from typing import Protocol

from nygen_router.types import RouterRequest, RouterResponse


class ProviderAdapter(Protocol):
    def invoke(self, request: RouterRequest) -> RouterResponse:
        """Send the request to one provider and return a normalized response."""
        ...
