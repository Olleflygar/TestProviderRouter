from __future__ import annotations


class NygenRouterError(Exception):
    """Base class for package-specific errors."""


class ConfigError(NygenRouterError):
    """Raised when router configuration is invalid."""


class MissingApiKeyError(ConfigError):
    def __init__(self, provider_name: str, env_var: str | None = None):
        self.provider_name = provider_name
        self.env_var = env_var
        detail = f" or environment variable {env_var!r}" if env_var else ""
        super().__init__(f"Missing API key for provider {provider_name!r}{detail}.")


class UnsupportedProtocolError(NygenRouterError):
    def __init__(self, protocol: object):
        self.protocol = protocol
        super().__init__(f"Unsupported provider protocol: {protocol!r}.")


class ProviderError(NygenRouterError):
    def __init__(self, provider_name: str, message: str):
        self.provider_name = provider_name
        self.message = message
        super().__init__(f"Provider {provider_name!r} failed: {message}")


class ProviderHTTPError(ProviderError):
    def __init__(self, provider_name: str, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(provider_name, f"HTTP {status_code}: {message}")


class ProviderResponseError(ProviderError):
    """Raised when a provider returns an unexpected response shape."""


class NoProvidersConfiguredError(ConfigError):
    """Raised when the router has no providers to call."""
