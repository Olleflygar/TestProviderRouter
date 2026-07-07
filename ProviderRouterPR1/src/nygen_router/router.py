from __future__ import annotations

from collections.abc import Callable

from nygen_router.adapters.base import ProviderAdapter
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.errors import (
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
    UnsupportedProtocolError,
)
from nygen_router.filters import filter_eligible_providers
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
    ):
        self.providers = list(providers)
        self._adapter_factory = adapter_factory or self._default_adapter_for

    def invoke(self, input: str | RouterRequest) -> RouterResponse:
        """Filter to eligible providers, call the first survivor, and report both."""
        request = RouterRequest.from_input(input)
        if not self.providers:
            raise NoProvidersConfiguredError("No providers configured.")

        eligible, excluded = filter_eligible_providers(
            self.providers, request, supported_protocols=SUPPORTED_PROTOCOLS
        )
        if not eligible:
            raise NoEligibleProvidersError(excluded)

        # PR2 selects the first survivor in config order; rotation is PR3.
        provider = eligible[0]
        adapter = self._adapter_for(provider)
        response = adapter.invoke(request)

        attempt = ProviderAttempt(provider_name=provider.name, success=True)
        return response.model_copy(update={"attempts": [attempt], "excluded": excluded})

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
