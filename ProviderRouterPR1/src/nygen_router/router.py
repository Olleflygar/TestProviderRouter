from __future__ import annotations

from collections.abc import Callable, Collection

from nygen_router.adapters.base import ProviderAdapter
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.errors import (
    ErrorCategory,
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
    RouterExhaustedError,
    UnsupportedProtocolError,
    categorize_error,
)
from nygen_router.filters import filter_eligible_providers
from nygen_router.health import ProviderHealthState
from nygen_router.policies import Policy, RoundRobinPolicy
from nygen_router.types import ProviderAttempt, RouterRequest, RouterResponse

AdapterFactory = Callable[[ProviderConfig], ProviderAdapter]

# Protocols the built-in adapter factory can serve. Adding a new adapter
# (e.g. OPENAI_RESPONSES in PR12) means registering its protocol here so the
# eligibility filter stops excluding it.
SUPPORTED_PROTOCOLS = frozenset({ApiProtocol.OPENAI_CHAT})


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

    def invoke(self, input: str | RouterRequest) -> RouterResponse:
        """Filter, order eligible providers, then try them in turn with fallback."""
        request = RouterRequest.from_input(input)
        if not self.providers:
            raise NoProvidersConfiguredError("No providers configured.")

        eligible, excluded = filter_eligible_providers(
            self.providers,
            request,
            supported_protocols=self._supported_protocols,
            disabled_this_run=self._auth_disabled_names(),
        )
        if not eligible:
            raise NoEligibleProvidersError(excluded)

        attempts: list[ProviderAttempt] = []
        for provider in self._policy.order(eligible):
            adapter = self._adapter_for(provider)
            try:
                response = adapter.invoke(request)
            except Exception as exc:
                attempts.append(
                    ProviderAttempt(provider_name=provider.name, success=False, error=exc)
                )
                category = categorize_error(exc)
                if category is ErrorCategory.AUTH:
                    # Bench it for the rest of the run; the filter excludes it
                    # on the next invoke() call, not this one.
                    self._health[provider.name] = ProviderHealthState(auth_disabled=True)
                if category is ErrorCategory.BAD_REQUEST:
                    # A 400 is almost always the request itself, not this
                    # provider -- stop rather than obscure it with more failures.
                    break
                continue

            attempts.append(ProviderAttempt(provider_name=provider.name, success=True))
            return response.model_copy(update={"attempts": attempts, "excluded": excluded})

        raise RouterExhaustedError(attempts)

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
