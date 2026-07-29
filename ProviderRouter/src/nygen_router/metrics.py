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

    ``stream`` means a stream actually opened, not that the caller asked for
    one: a streaming call that dies before the response arrives has no stream
    to observe and is recorded like any other failed attempt. On a stream row
    ``latency_ms`` is time-to-first-chunk -- never a full-response duration --
    and is None when no chunk ever arrived; ``total_duration_ms`` spans the
    attempt from start to the stream's end, whether that end was completion or
    death mid-generation.
    """

    provider_name: str
    model: str
    protocol: ApiProtocol
    success: bool
    latency_ms: float | None = None
    error_type: str | None = None
    stream: bool = False
    total_duration_ms: float | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
