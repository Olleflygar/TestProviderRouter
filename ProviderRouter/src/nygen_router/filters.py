from __future__ import annotations

from collections.abc import Collection, Mapping
from types import MappingProxyType

from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.errors import MissingApiKeyError
from nygen_router.health import CooldownTrigger, ProviderHealthState
from nygen_router.types import EligibilityResult, FilterReason

# Default for callers with no health state to consult (the filter never writes,
# so an immutable mapping is both safe as a default and honest about intent).
_NO_HEALTH: Mapping[str, ProviderHealthState] = MappingProxyType({})


def filter_eligible_providers(
    providers: list[ProviderConfig],
    *,
    supported_protocols: Collection[ApiProtocol],
    requested_protocols: Collection[ApiProtocol],
    health: Mapping[str, ProviderHealthState] = _NO_HEALTH,
    now: float = 0.0,
) -> tuple[list[ProviderConfig], list[EligibilityResult]]:
    """Split providers into those that can satisfy this call and those that cannot.

    Hard filters, not scores: a provider that fails any essential check is
    excluded with a specific FilterReason, not ranked lower. Each excluded
    provider yields exactly one EligibilityResult carrying its first failing
    reason. ``requested_protocols`` is the set of protocols present in this
    call's CallVariants -- a provider whose protocol isn't among them is
    excluded from this call, even if the router supports that protocol in
    general. ``health`` is the router's live health state and ``now`` a reading
    of its monotonic clock; both are only read here -- benching and expiry are
    the router's business, so an elapsed cooldown is treated as eligible
    without being cleared.
    """
    eligible: list[ProviderConfig] = []
    excluded: list[EligibilityResult] = []
    for provider in providers:
        exclusion = _first_failing_reason(
            provider, supported_protocols, requested_protocols, health, now
        )
        if exclusion is None:
            eligible.append(provider)
        else:
            reason, detail = exclusion
            excluded.append(
                EligibilityResult(provider_name=provider.name, reason=reason, detail=detail)
            )
    return eligible, excluded


def _first_failing_reason(
    provider: ProviderConfig,
    supported_protocols: Collection[ApiProtocol],
    requested_protocols: Collection[ApiProtocol],
    health: Mapping[str, ProviderHealthState],
    now: float,
) -> tuple[FilterReason, str] | None:
    """Return the first essential filter this provider fails, or None if eligible."""
    if not provider.enabled:
        return FilterReason.DISABLED, "provider is disabled"
    state = health.get(provider.name)
    if state is not None and state.auth_disabled:
        return FilterReason.AUTH_DISABLED_THIS_RUN, _auth_disabled_detail(state)
    if state is not None:
        remaining = state.cooldown_remaining(now)
        if remaining is not None:
            return FilterReason.IN_COOLDOWN, _cooldown_detail(state, remaining)
    if not _has_resolvable_api_key(provider):
        return FilterReason.MISSING_API_KEY, "no API key available"
    if provider.protocol not in supported_protocols:
        return FilterReason.UNSUPPORTED_PROTOCOL, f"protocol {provider.protocol} is not supported"
    if provider.protocol not in requested_protocols:
        return (
            FilterReason.NO_MATCHING_CALL_VARIANT,
            f"no CallVariant for protocol {provider.protocol} was supplied to this call",
        )
    return None


def _auth_disabled_detail(state: ProviderHealthState) -> str:
    """Name the auth bench, carrying the provider's own error text verbatim."""
    detail = "disabled after an auth failure earlier this run"
    if state.last_error is None:
        return detail
    return f"{detail}; last error: {state.last_error}"


def _cooldown_detail(state: ProviderHealthState, remaining: float) -> str:
    """Name the cooldown's real trigger, its remaining time, and the verbatim last error.

    One FilterReason covers both triggers; this detail is what tells them
    apart, so a fully benched router still enumerates root causes rather than
    repeating "in cooldown".
    """
    if state.cooldown_trigger is CooldownTrigger.RATE_LIMIT:
        trigger = "after rate limiting"
    else:
        trigger = f"after {state.consecutive_failures} consecutive failures"
    detail = f"in cooldown ({remaining:.1f}s remaining) {trigger}"
    if state.last_error is None:
        return detail
    return f"{detail}; last error: {state.last_error}"


def _has_resolvable_api_key(provider: ProviderConfig) -> bool:
    """Check the key can be resolved without raising or exposing its value."""
    try:
        provider.resolve_api_key()
    except MissingApiKeyError:
        return False
    return True
