from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from llm_provider_router.config import ApiProtocol
from llm_provider_router.types import CallType


@dataclass(frozen=True)
class MetricsEvent:
    """One observational record of a single provider attempt.

    An internal record (dataclass, not Pydantic) per Core design principle 7.
    ``timestamp`` is always timezone-aware UTC. ``error_type`` is the
    ErrorCategory value string on failure, None on success.

    ``call_type`` is caller-declared router metadata. ``stream_opened`` records
    whether the router observed a NormalizedStream. On a streaming row
    ``latency_ms`` is time-to-first-chunk -- never a full-response duration --
    and is None when no chunk ever arrived; ``total_duration_ms`` spans the
    attempt from start to the stream's end, whether that end was completion or
    death mid-generation.
    """

    metrics_scope: str
    provider_id: str
    provider_name: str
    model: str
    protocol: ApiProtocol
    call_type: CallType
    success: bool
    stream_opened: bool | None = None
    latency_ms: float | None = None
    total_duration_ms: float | None = None
    error_type: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
