from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx


class NygenRouterError(Exception):
    """Base class for every error raised by nygen-router.

    ``except NygenRouterError`` catches anything the router itself raises.
    Transport and HTTP failures always chain the underlying exception via
    ``raise ... from original`` so the real cause is never hidden and never
    re-wrapped a second time.
    """


class ConfigError(NygenRouterError):
    """Invalid configuration, detected before any request is sent."""


class MissingApiKeyError(ConfigError):
    def __init__(self, provider_name: str, env_var: str | None = None) -> None:
        """Build a message hinting how to supply the missing key."""
        self.provider_name = provider_name
        self.env_var = env_var
        if env_var:
            hint = f"pass api_key=... to ProviderConfig or set the {env_var!r} environment variable"
        else:
            hint = "pass api_key=... to ProviderConfig or set api_key_env to a populated variable"
        super().__init__(f"No API key for provider {provider_name!r}: {hint}.")


class UnsupportedProtocolError(NygenRouterError):
    def __init__(self, provider_name: str, protocol: object) -> None:
        """Flag a provider configured with a protocol no adapter handles yet."""
        self.provider_name = provider_name
        self.protocol = protocol
        super().__init__(
            f"Provider {provider_name!r} uses protocol {str(protocol)!r}, which is not "
            f"supported yet (PR 1 supports only 'openai_chat')."
        )


class CapabilityError(NygenRouterError):
    """The request needs a capability the chosen provider does not declare."""

    def __init__(self, provider_name: str, capability: str) -> None:
        self.provider_name = provider_name
        self.capability = capability
        super().__init__(
            f"Provider {provider_name!r} does not support {capability}, "
            f"which this request requires."
        )


class NoProvidersConfiguredError(ConfigError):
    """No usable provider was available when routing a request."""


class ProviderError(NygenRouterError):
    """A call to a provider failed at the transport level.

    The original exception (e.g. ``httpx.ConnectError``) is preserved both as
    ``__cause__`` (via ``raise ... from``) and on ``.original``.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_name: str,
        model: str,
        original: BaseException | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self.original = original
        super().__init__(message)


class ProviderTimeoutError(ProviderError):
    """A provider request exceeded its configured timeout."""


class ProviderConnectionError(ProviderError):
    """A provider request could not establish a connection."""


class ProviderHTTPError(ProviderError):
    """A provider returned a non-2xx HTTP status.

    The provider's verbatim error message and structured fields are surfaced
    directly; no unwrapping is needed to read them. The standard
    ``httpx.HTTPStatusError`` remains available as ``__cause__`` and the raw
    response as ``.response``.
    """

    def __init__(
        self,
        *,
        provider_name: str,
        model: str,
        status_code: int,
        message: str,
        error_type: str | None = None,
        error_code: str | None = None,
        body: Any = None,
        response: httpx.Response | None = None,
        original: BaseException | None = None,
    ) -> None:
        """Format a message combining status, provider's error text, and type/code tags."""
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.error_code = error_code
        self.body = body
        self.response = response

        reason = _http_reason(status_code)
        status = f"HTTP {status_code} {reason}".rstrip()
        detail = message or "<no error message in provider response body>"
        tags = "/".join(tag for tag in (error_type, error_code) if tag)
        tag_suffix = f" [{tags}]" if tags else ""
        super().__init__(
            f"Provider {provider_name!r} returned {status} for model {model!r}: "
            f"{detail}{tag_suffix}",
            provider_name=provider_name,
            model=model,
            original=original,
        )


class ProviderResponseError(ProviderError):
    """A provider returned a 2xx response the router could not interpret."""

    def __init__(
        self,
        provider_name: str,
        model: str,
        problem: str,
        *,
        body: Any = None,
        response: httpx.Response | None = None,
    ) -> None:
        self.problem = problem
        self.body = body
        self.response = response
        super().__init__(
            f"Provider {provider_name!r} returned an unexpected response for model "
            f"{model!r}: {problem}",
            provider_name=provider_name,
            model=model,
        )


def _http_reason(status_code: int) -> str:
    """Map a status code to its standard reason phrase, or "" if unknown."""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return ""
