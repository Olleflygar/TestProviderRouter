from __future__ import annotations

from nygen_router.adapters.base import ProviderAdapter
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.capabilities import validate_request_capabilities
from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.errors import NoProvidersConfiguredError, UnsupportedProtocolError
from nygen_router.types import RouterRequest, RouterResponse


class ProviderRouter:
    def __init__(self, providers: list[ProviderConfig]):
        self.providers = list(providers)

    def invoke(self, input: str | RouterRequest) -> RouterResponse:
        request = RouterRequest.from_input(input)
        provider = self._first_enabled_provider()
        validate_request_capabilities(provider, request)
        adapter = self._adapter_for(provider)
        return adapter.invoke(request)

    def _first_enabled_provider(self) -> ProviderConfig:
        if not self.providers:
            raise NoProvidersConfiguredError("No providers configured.")

        for provider in self.providers:
            if provider.enabled:
                return provider

        raise NoProvidersConfiguredError("No enabled providers configured.")

    def _adapter_for(self, provider: ProviderConfig) -> ProviderAdapter:
        if provider.protocol == ApiProtocol.OPENAI_CHAT:
            return OpenAICompatibleAdapter(provider)
        raise UnsupportedProtocolError(provider.name, provider.protocol)
