from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RouterResult:
    content: str
    provider: str
    model: str
    raw: Any
    metadata: dict = field(default_factory=dict)

    @property
    def output(self) -> str:
        """Pydantic AI convention; alias to content."""
        return self.content

    def __str__(self) -> str:
        return self.content
