from __future__ import annotations

from abc import ABC, abstractmethod


class BaseState(ABC):
    @abstractmethod
    def get_next_provider(self, providers: list[str]) -> str:
        """Return the name of the next provider and advance the counter."""
