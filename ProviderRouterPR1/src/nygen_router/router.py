from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

from nygen_router.adapters.base import ProviderAdapter
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.errors import (
    DuplicateCallVariantProtocolError,
    ErrorCategory,
    ModelArgumentConflictError,
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
    RouterExhaustedError,
    UnsupportedProtocolError,
    categorize_error,
)
from nygen_router.filters import filter_eligible_providers
from nygen_router.health import ProviderHealthState
from nygen_router.policies import Policy, RoundRobinPolicy
from nygen_router.types import CallVariant, ProviderAttempt

AdapterFactory = Callable[[ProviderConfig], ProviderAdapter]

# Protocols the built-in adapter factory can serve. Adding a new adapter
# (e.g. OPENAI_RESPONSES in PR12) means registering its protocol here so the
# eligibility filter stops excluding it.
SUPPORTED_PROTOCOLS = frozenset({ApiProtocol.OPENAI_CHAT})

# Failure categories that abort the whole run immediately instead of falling
# back to the next eligible provider: the call itself is broken (malformed
# request, bad operation, mismatched arguments, missing SDK), so no other
# provider trying the same broken call would fare any better.
_STOP_CATEGORIES = frozenset({ErrorCategory.BAD_REQUEST, ErrorCategory.INVALID_OPERATION})


class ProviderRouter:
    def __init__(
        self,
        providers: list[ProviderConfig],
        adapter_factory: AdapterFactory | None = None,
        policy: Policy | None = None,
        supported_protocols: Collection[ApiProtocol] | None = None,
    ):
        self.providers = list(providers)
        self._adapter_factory = adapter_factory or self._default_adapter_for
        self._policy = policy or RoundRobinPolicy()
        # A custom adapter_factory that serves more protocols must pass the
        # matching set here, or the eligibility filter keeps excluding them.
        self._supported_protocols = (
            frozenset(supported_protocols)
            if supported_protocols is not None
            else SUPPORTED_PROTOCOLS
        )
        # Per-run provider health, visible to the eligibility filter and any
        # policy. Not persisted across process restarts (PR3 scope).
        self._health: dict[str, ProviderHealthState] = {}

    def invoke(self, calls: list[CallVariant]) -> Any:
        """Filter, order eligible providers, then try them in turn with fallback.

        Returns the raw native response object the winning provider's SDK
        returned -- untouched, with nothing attached. Every attempt and
        exclusion is still tracked internally, so a total failure still
        raises an error enumerating each provider's own real reason.
        """
        if not self.providers:
            raise NoProvidersConfiguredError("No providers configured.")

        variants_by_protocol = self._prepare_variants(calls)

        eligible, excluded = filter_eligible_providers(
            self.providers,
            supported_protocols=self._supported_protocols,
            requested_protocols=variants_by_protocol.keys(),
            disabled_this_run=self._auth_disabled_names(),
        )
        if not eligible:
            raise NoEligibleProvidersError(excluded)

        attempts: list[ProviderAttempt] = []
        for provider in self._policy.order(eligible):
            variant = variants_by_protocol[provider.protocol]
            arguments = {**variant.arguments, "model": provider.model}
            adapter = self._adapter_for(provider)
            try:
                response = adapter.invoke(variant.operation, arguments)
            except Exception as exc:
                attempts.append(
                    ProviderAttempt(provider_name=provider.name, success=False, error=exc)
                )
                category = categorize_error(exc)
                if category is ErrorCategory.AUTH:
                    # Bench it for the rest of the run; the filter excludes it
                    # on the next invoke() call, not this one.
                    self._health[provider.name] = ProviderHealthState(auth_disabled=True)
                if category in _STOP_CATEGORIES:
                    break
                continue

            attempts.append(ProviderAttempt(provider_name=provider.name, success=True))
            return response

        raise RouterExhaustedError(attempts)

    @staticmethod
    def _prepare_variants(calls: list[CallVariant]) -> dict[ApiProtocol, CallVariant]:
        """Validate every CallVariant once, upfront, before any provider is contacted.

        Never mutates a CallVariant's arguments -- the same variant is reused
        across every provider attempt of its protocol in the fallback loop, so
        the model-conflict check runs once here, against the caller's
        original arguments only.
        """
        variants_by_protocol: dict[ApiProtocol, CallVariant] = {}
        for call in calls:
            if call.protocol in variants_by_protocol:
                raise DuplicateCallVariantProtocolError(call.protocol)
            variants_by_protocol[call.protocol] = call
        for variant in variants_by_protocol.values():
            if "model" in variant.arguments:
                raise ModelArgumentConflictError(variant.protocol, variant.operation)
        return variants_by_protocol

    def _auth_disabled_names(self) -> frozenset[str]:
        """Names of providers benched this run for an auth failure."""
        return frozenset(name for name, state in self._health.items() if state.auth_disabled)

    def _adapter_for(self, provider: ProviderConfig) -> ProviderAdapter:
        return self._adapter_factory(provider)

    @staticmethod
    def _default_adapter_for(provider: ProviderConfig) -> ProviderAdapter:
        """Map a provider's protocol to its adapter (only OPENAI_CHAT exists so far)."""
        if provider.protocol == ApiProtocol.OPENAI_CHAT:
            return OpenAICompatibleAdapter(provider)
        # Unreachable via invoke(): unsupported protocols are excluded by the
        # eligibility filter first. Kept as a guard for direct/custom callers.
        raise UnsupportedProtocolError(provider.name, provider.protocol)  # pragma: no cover
