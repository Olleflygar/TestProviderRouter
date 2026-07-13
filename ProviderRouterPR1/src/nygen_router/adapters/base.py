from __future__ import annotations

from typing import Any, Protocol


class ProviderAdapter(Protocol):
    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        """Dispatch operation/arguments to one provider and return its native response."""
        ...
