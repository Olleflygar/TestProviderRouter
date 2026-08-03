from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field

from nygen_router.config import ProviderConfig
from nygen_router.errors import ErrorCategory
from nygen_router.metrics import MetricsEvent
from nygen_router.types import CallType


@dataclass(frozen=True)
class ProviderStats:
    """What one provider's recent history says about it, ready for scoring.

    An internal record (dataclass, not Pydantic) per Core design principle 7.

    Regular and streaming calls are kept apart rather than blended, because the
    two are not the same measurement: a regular attempt's latency is the time
    to a whole response, a streaming attempt's is the time to its first chunk,
    and the two succeed at different moments. Averaging them together would
    hide exactly the operation-specific weakness a streaming-heavy workflow
    needs to see. A provider with populated regular figures and all-zero
    streaming ones (or the reverse) is normal.

    The four count fields are ``float`` because a weighted aggregation --
    recency decay, for instance -- makes them fractional; under the default
    flat weighting they are whole numbers. The three tallies stay ``int`` --
    exact, unweighted, diagnostic only, never read by scoring and never
    decayed.

    A ``*_success_rate`` is None exactly when its attempt count is zero, and a
    ``*_avg_latency_ms`` is None when there is no successful attempt carrying a
    latency to average: no evidence is reported as no evidence, never as 0.0.
    """

    provider_id: str
    provider_name: str
    regular_attempt_count: float
    regular_success_count: float
    regular_success_rate: float | None
    regular_avg_latency_ms: float | None
    streaming_attempt_count: float
    streaming_success_count: float
    streaming_success_rate: float | None
    streaming_avg_ttft_ms: float | None
    recent_error_count: int
    rate_limit_count: int
    timeout_count: int


def aggregate_stats(
    events: Sequence[MetricsEvent],
    providers: Collection[ProviderConfig],
    call_type: CallType,
    *,
    weight_fn: Callable[[MetricsEvent], float] | None = None,
) -> dict[str, ProviderStats]:
    """Summarize recorded attempts into one ProviderStats per requested provider.

    Query-only: whatever window, model, or provider filtering the caller wants
    happened before this, in ``MetricsStore.query_recent``. Aggregation lives
    here in Python rather than in per-backend SQL, so a custom backend stays
    trivial to implement.

    Every provider ID in ``providers`` gets an entry, including one with no
    matching events at all -- the same "every configured provider is reported"
    rule ``health_report()`` follows, so a brand-new provider is a real entry
    with no evidence rather than a missing key its caller has to special-case.
    Events from any other provider are ignored.

    ``weight_fn`` decides how much each event counts for, which is how a
    caller applies recency decay. With the default every event weighs 1.0,
    which is plain counting.
    """
    weight_of = _flat_weight if weight_fn is None else weight_fn
    configured = {provider.provider_id: provider for provider in providers}
    accumulators = {provider_id: _Accumulator() for provider_id in configured}
    for event in events:
        provider = configured.get(event.provider_id)
        if provider is None or not _matches_partition(event, provider, call_type):
            continue
        accumulator = accumulators[event.provider_id]
        accumulator.add(event, weight_of(event))
    return {
        provider_id: accumulator.build(configured[provider_id])
        for provider_id, accumulator in accumulators.items()
    }


def _matches_partition(event: MetricsEvent, provider: ProviderConfig, call_type: CallType) -> bool:
    return (
        event.provider_id == provider.provider_id
        and event.model == provider.model
        and event.protocol == provider.protocol
        and event.call_type == call_type
    )


def _flat_weight(event: MetricsEvent) -> float:
    """Count every event once -- the trivial weighting, used when none is supplied."""
    return 1.0


@dataclass
class _Bucket:
    """Running totals for one provider's attempts of one call type."""

    attempts: float = 0.0
    successes: float = 0.0
    latency_weight: float = 0.0
    latency_total: float = 0.0

    def add(self, event: MetricsEvent, weight: float) -> None:
        self.attempts += weight
        if not event.success:
            # Latency is deliberately not collected from failures: a provider
            # that fails fast must never come out looking fast.
            return
        self.successes += weight
        if event.latency_ms is None:
            # A completed stream that never yielded a chunk has no
            # time-to-first-chunk; counting it as 0.0 would make the emptiest
            # response the fastest one on record.
            return
        self.latency_weight += weight
        self.latency_total += weight * event.latency_ms

    @property
    def success_rate(self) -> float | None:
        return None if self.attempts == 0 else self.successes / self.attempts

    @property
    def avg_latency_ms(self) -> float | None:
        return None if self.latency_weight == 0 else self.latency_total / self.latency_weight


@dataclass
class _Accumulator:
    """One provider's totals: a bucket per call type, plus the diagnostic tallies."""

    regular: _Bucket = field(default_factory=_Bucket)
    streaming: _Bucket = field(default_factory=_Bucket)
    errors: int = 0
    rate_limits: int = 0
    timeouts: int = 0

    def add(self, event: MetricsEvent, weight: float) -> None:
        bucket = self.streaming if event.call_type is CallType.STREAMING else self.regular
        bucket.add(event, weight)
        if event.success:
            return
        # Exact tallies across both call types, never weighted: these are read
        # by a human diagnosing a provider, not by the score calculator.
        self.errors += 1
        if event.error_type == ErrorCategory.RATE_LIMIT.value:
            self.rate_limits += 1
        elif event.error_type == ErrorCategory.TIMEOUT.value:
            self.timeouts += 1

    def build(self, provider: ProviderConfig) -> ProviderStats:
        return ProviderStats(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            regular_attempt_count=self.regular.attempts,
            regular_success_count=self.regular.successes,
            regular_success_rate=self.regular.success_rate,
            regular_avg_latency_ms=self.regular.avg_latency_ms,
            streaming_attempt_count=self.streaming.attempts,
            streaming_success_count=self.streaming.successes,
            streaming_success_rate=self.streaming.success_rate,
            streaming_avg_ttft_ms=self.streaming.avg_latency_ms,
            recent_error_count=self.errors,
            rate_limit_count=self.rate_limits,
            timeout_count=self.timeouts,
        )
