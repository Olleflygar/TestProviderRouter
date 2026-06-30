from __future__ import annotations

from .base import BaseState


class InMemoryState(BaseState):
    def __init__(self):
        self._index = 0

    def get_next_provider(self, providers: list[str]) -> str:
        provider = providers[self._index % len(providers)]
        self._index += 1
        return provider
