from __future__ import annotations


class RouterError(Exception):
    """Base exception for all router-level errors."""


class NoProvidersAvailable(RouterError):
    """Raised when no providers are available for a call."""


class MissingProviderKey(RouterError):
    """Raised lazily when the selected provider needs an API key."""

    def __init__(self, provider: str, env_var: str):
        self.provider = provider
        self.env_var = env_var
        super().__init__(
            f"Provider '{provider}' is missing an API key. "
            f"Pass providers={{'{provider}': {{'api_key': '...'}}}} or set {env_var}."
        )


class ProviderError(RouterError):
    """Wraps a provider SDK exception without swallowing it."""

    def __init__(self, provider: str, model: str, original: Exception):
        self.provider = provider
        self.model = model
        self.original = original
        super().__init__(
            f"Provider '{provider}' failed for model '{model}': {original}"
        )
