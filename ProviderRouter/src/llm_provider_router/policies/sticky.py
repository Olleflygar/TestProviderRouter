from __future__ import annotations

from collections import Counter
from collections.abc import Collection

from llm_provider_router.config import ProviderConfig
from llm_provider_router.errors import ConfigError
from llm_provider_router.policies.base import Policy, RoutingContext
from llm_provider_router.policies.round_robin import RoundRobinPolicy


class StickyRoutingPolicy:
    """Prefer configured provider IDs, then delegate the eligible remainder.

    This is fixed, router-wide preference rather than learned affinity. Hard
    eligibility filtering happens before ``order`` and always takes priority.
    """

    def __init__(
        self,
        *,
        sticky_provider_ids: list[str],
        fallback_policy: Policy | None = None,
    ) -> None:
        if not isinstance(sticky_provider_ids, list):
            raise ConfigError("sticky_provider_ids must be a list of provider ID strings")
        if not sticky_provider_ids:
            raise ConfigError("sticky_provider_ids must contain at least one provider ID")

        normalized: list[str] = []
        for provider_id in sticky_provider_ids:
            if not isinstance(provider_id, str):
                raise ConfigError("Sticky provider IDs must be strings")
            value = provider_id.strip()
            if not value:
                raise ConfigError("Sticky provider IDs must not be empty or whitespace-only")
            normalized.append(value)

        counts = Counter(normalized)
        duplicates = sorted(provider_id for provider_id, count in counts.items() if count > 1)
        if duplicates:
            rendered = ", ".join(repr(provider_id) for provider_id in duplicates)
            raise ConfigError(f"Duplicate sticky provider ID(s): {rendered}.")

        # Keep an independent normalized list so caller mutation cannot alter
        # routing behavior after construction.
        self._sticky_provider_ids = list(normalized)
        self._fallback_policy = RoundRobinPolicy() if fallback_policy is None else fallback_policy

    def validate_provider_ids(self, configured_provider_ids: Collection[str]) -> None:
        """Reject sticky IDs that are not configured on the owning router."""
        configured = set(configured_provider_ids)
        unknown = [
            provider_id
            for provider_id in self._sticky_provider_ids
            if provider_id not in configured
        ]
        if unknown:
            rendered = ", ".join(repr(provider_id) for provider_id in unknown)
            raise ConfigError(f"Unknown sticky provider ID(s): {rendered}.")

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        """Return the eligible fixed prefix followed by wrapped-policy order."""
        eligible_by_id = {provider.provider_id: provider for provider in eligible}
        sticky_ids = set(self._sticky_provider_ids)
        sticky_prefix = [
            eligible_by_id[provider_id]
            for provider_id in self._sticky_provider_ids
            if provider_id in eligible_by_id
        ]
        remainder = [provider for provider in eligible if provider.provider_id not in sticky_ids]
        remainder_by_id = {provider.provider_id: provider for provider in remainder}

        ordered_remainder = self._fallback_policy.order(list(remainder), context)
        validated_remainder: list[ProviderConfig] = []
        for returned in ordered_remainder:
            canonical = remainder_by_id.get(returned.provider_id)
            if canonical is None:
                raise ConfigError(
                    "Sticky fallback policy returned provider ID "
                    f"{returned.provider_id!r}, which was not in its eligible "
                    "non-sticky remainder."
                )
            validated_remainder.append(canonical)

        return sticky_prefix + validated_remainder
