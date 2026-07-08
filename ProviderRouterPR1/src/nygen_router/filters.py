from __future__ import annotations

from collections.abc import Collection

from nygen_router.capabilities import missing_capability
from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.errors import MissingApiKeyError
from nygen_router.types import EligibilityResult, FilterReason, RouterRequest


def filter_eligible_providers(
    providers: list[ProviderConfig],
    request: RouterRequest,
    *,
    supported_protocols: Collection[ApiProtocol],
    disabled_this_run: Collection[str] = frozenset(),
) -> tuple[list[ProviderConfig], list[EligibilityResult]]:
    """Split providers into those that can satisfy the request and those that cannot.

    Hard filters, not scores: a provider that fails any essential check is
    excluded with a specific FilterReason, not ranked lower. Each excluded
    provider yields exactly one EligibilityResult carrying its first failing
    reason. ``disabled_this_run`` names providers benched for the current run
    (e.g. after an auth failure); the router supplies it from its health state.
    """
    eligible: list[ProviderConfig] = []
    excluded: list[EligibilityResult] = []
    for provider in providers:
        exclusion = _first_failing_reason(provider, request, supported_protocols, disabled_this_run)
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
    request: RouterRequest,
    supported_protocols: Collection[ApiProtocol],
    disabled_this_run: Collection[str],
) -> tuple[FilterReason, str] | None:
    """Return the first essential filter this provider fails, or None if eligible."""
    if not provider.enabled:
        return FilterReason.DISABLED, "provider is disabled"
    if provider.name in disabled_this_run:
        return (
            FilterReason.AUTH_DISABLED_THIS_RUN,
            "disabled after an auth failure earlier this run",
        )
    if not _has_resolvable_api_key(provider):
        return FilterReason.MISSING_API_KEY, "no API key available"
    if provider.protocol not in supported_protocols:
        return FilterReason.UNSUPPORTED_PROTOCOL, f"protocol {provider.protocol} is not supported"
    return missing_capability(provider, request)


def _has_resolvable_api_key(provider: ProviderConfig) -> bool:
    """Check the key can be resolved without raising or exposing its value."""
    try:
        provider.resolve_api_key()
    except MissingApiKeyError:
        return False
    return True
