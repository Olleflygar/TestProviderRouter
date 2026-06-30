from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from providerrouter.result import RouterResult


class BaseProvider(ABC):
    name: str
    supported_models: list[str]

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        env_var: str | None = None,
        config: dict | None = None,
    ):
        self.api_key = api_key
        self.api_base = api_base
        self.env_var = env_var
        self.config = config or {}

    @abstractmethod
    def call(self, model: str, messages: list[dict], **kwargs) -> "RouterResult":
        """Make the blocking API call and return a RouterResult."""

    @abstractmethod
    async def acall(self, model: str, messages: list[dict], **kwargs) -> "RouterResult":
        """Async version of call()."""

    def supports_model(self, model: str) -> bool:
        """Return True if this provider can serve the requested model."""
        if not self.supported_models:
            return True
        normalized = model.lower()
        return any(fragment.lower() in normalized for fragment in self.supported_models)
