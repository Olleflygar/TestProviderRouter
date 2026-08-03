from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from nygen_router.errors import ErrorCategory


class HealthConfig(BaseModel):
    """Thresholds and durations governing when a provider is benched.

    The two cooldown durations share a default on purpose; they are separate
    knobs so a deployment can let rate-limit and failure benches diverge.
    """

    model_config = ConfigDict(extra="forbid")

    rate_limit_cooldown_seconds: float = 60.0
    failure_cooldown_seconds: float = 60.0
    failure_threshold: int = 3

    @field_validator("rate_limit_cooldown_seconds", "failure_cooldown_seconds")
    @classmethod
    def _duration_must_be_positive(cls, value: float) -> float:
        """Reject zero/negative cooldowns -- a bench that expires instantly is not a bench."""
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator("failure_threshold")
    @classmethod
    def _threshold_must_be_at_least_one(cls, value: int) -> int:
        """Reject a zero threshold, which would bench a provider before it ever failed."""
        if value < 1:
            raise ValueError("must be at least 1")
        return value


class CooldownTrigger(StrEnum):
    """What put a provider in its current cooldown.

    Stored when the bench is taken rather than inferred later: the failure
    count alone cannot distinguish the two, because a rate limit neither
    increments nor resets it -- a provider already at the failure threshold
    that then gets a 429 would otherwise misreport its real trigger.
    """

    RATE_LIMIT = "rate_limit"
    CONSECUTIVE_FAILURES = "consecutive_failures"


# Categories that count toward consecutive_failures. UNKNOWN counts on
# purpose: its real families -- provider-specific HTTP statuses (404 from a
# wrong base_url or a model this provider doesn't host, 413 payload limits)
# and the defensive catch-all -- are both per-provider problems. Caller-side
# mistakes never reach here; they are STOP categories the router acts on
# before health is involved. STREAM_INTERRUPTED counts for the same reason:
# a provider that chronically truncates its streams is a broken provider,
# even though every one of its calls starts out looking healthy.
_COUNTED_CATEGORIES = frozenset(
    {
        ErrorCategory.TIMEOUT,
        ErrorCategory.SERVER_ERROR,
        ErrorCategory.CONNECTION,
        ErrorCategory.STREAM_INTERRUPTED,
        ErrorCategory.UNKNOWN,
    }
)


@dataclass
class ProviderHealthState:
    """Per-run health state for a single provider, held on the router.

    Lives on ProviderRouter (not inside a policy) so the same state is visible
    to the eligibility filter and to any policy, and is not lost if the policy
    is swapped. Transitions belong here rather than in the router's fallback
    loop: the loop only reports what happened and reads back whether a bench
    began.

    All times are values from the router's injected monotonic clock, never
    wall-clock timestamps.
    """

    auth_disabled: bool = False
    cooldown_until: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    cooldown_trigger: CooldownTrigger | None = None
    # Bench logging dedup, per decision 7: the first bench of a bench episode
    # warns, repeat benches within it stay at DEBUG, and the success that ends
    # the episode logs one recovery line. Recovery re-arms the warning, so a
    # later, separate outage is never silent.
    warned: bool = False
    benched: bool = False

    def record_failure(
        self,
        category: ErrorCategory,
        error_text: str,
        config: HealthConfig,
        now: float,
    ) -> bool:
        """Apply one failure to this provider's health; True if it started a new bench.

        Never called for the STOP categories: a malformed request or a bad
        operation is the call's fault, not the provider's, so its health is
        left untouched.
        """
        if category is ErrorCategory.AUTH:
            self.last_error = error_text
            self.auth_disabled = True
            self.benched = True
            return True

        if category is ErrorCategory.RATE_LIMIT:
            # Flow control, not "provider is off": a 429 neither increments nor
            # resets the failure count.
            self.last_error = error_text
            self.cooldown_until = now + config.rate_limit_cooldown_seconds
            self.cooldown_trigger = CooldownTrigger.RATE_LIMIT
            self.benched = True
            return True

        if category in _COUNTED_CATEGORIES:
            self.last_error = error_text
            self.consecutive_failures += 1
            if self.consecutive_failures >= config.failure_threshold:
                self.cooldown_until = now + config.failure_cooldown_seconds
                self.cooldown_trigger = CooldownTrigger.CONSECUTIVE_FAILURES
                self.benched = True
                return True
            return False

        return False

    def record_success(self) -> None:
        """Clear this provider's failure signal after a call it served.

        Ends the bench episode, re-arming the warning so a later, separate
        outage is reported at WARNING rather than buried at DEBUG. Callers that
        log the recovery must read ``benched`` before calling this.

        Leaves auth_disabled alone: an auth bench lasts the whole run and only
        reset_health lifts it (a benched provider is filtered out, so it cannot
        reach here anyway).
        """
        self.consecutive_failures = 0
        self.cooldown_until = None
        self.cooldown_trigger = None
        self.last_error = None
        self.warned = False
        self.benched = False

    def cooldown_remaining(self, now: float) -> float | None:
        """Seconds left on the cooldown, or None once it has lapsed.

        Read-only: an expired cooldown_until is reported as "not benched" but
        never cleared here, so the eligibility filter can stay side-effect free.
        """
        if self.cooldown_until is None:
            return None
        remaining = self.cooldown_until - now
        return remaining if remaining > 0 else None


@dataclass(frozen=True)
class ProviderHealthReport:
    """A snapshot of one provider's health, safe to hold onto.

    Frozen, and built fresh per health_report() call, so no live router state
    escapes to a caller. Cooldowns are reported as remaining seconds rather
    than absolute deadlines, which are meaningless outside the router's clock.
    """

    provider_id: str
    provider_name: str
    auth_disabled: bool = False
    consecutive_failures: int = 0
    cooldown_remaining_seconds: float | None = None
    last_error: str | None = None
