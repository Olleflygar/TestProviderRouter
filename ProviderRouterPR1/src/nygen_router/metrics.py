from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from nygen_router.config import ApiProtocol


@dataclass(frozen=True)
class MetricsEvent:
    """One observational record of a single provider attempt.

    An internal record (dataclass, not Pydantic) per Core design principle 7.
    ``timestamp`` is always timezone-aware UTC. ``error_type`` is the
    ErrorCategory value string on failure, None on success.
    """

    provider_name: str
    model: str
    protocol: ApiProtocol
    success: bool
    latency_ms: float | None = None
    error_type: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
