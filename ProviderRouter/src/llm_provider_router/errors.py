from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from llm_provider_router.config import ApiProtocol
    from llm_provider_router.types import CallType, EligibilityResult, ProviderAttempt


class NygenRouterError(Exception):
    """Base class for every error raised by llm-provider-router.

    ``except NygenRouterError`` catches anything the router itself raises.
    Transport and HTTP failures always chain the underlying exception via
    ``raise ... from original`` so the real cause is never hidden and never
    re-wrapped a second time.
    """


class ConfigError(NygenRouterError):
    """Invalid configuration, detected before any request is sent."""


class RouterClosedError(NygenRouterError):
    """The router was closed; close() is terminal and a closed router serves no calls."""

    def __init__(self) -> None:
        super().__init__(
            "This ProviderRouter has been closed. close() is terminal: create a new "
            "router instead of reusing a closed one."
        )


def _provider_label(provider_name: str, provider_id: str) -> str:
    return f'Provider "{provider_name}" (id="{provider_id}")'


class MissingApiKeyError(ConfigError):
    def __init__(self, provider_id: str, provider_name: str, env_var: str | None = None) -> None:
        """Build a message hinting how to supply the missing key."""
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.env_var = env_var
        if env_var:
            hint = f"pass api_key=... to ProviderConfig or set the {env_var!r} environment variable"
        else:
            hint = "pass api_key=... to ProviderConfig or set api_key_env to a populated variable"
        super().__init__(f"No API key for {_provider_label(provider_name, provider_id)}: {hint}.")


class UnsupportedProtocolError(NygenRouterError):
    def __init__(self, provider_id: str, provider_name: str, protocol: object) -> None:
        """Flag a provider configured with a protocol no adapter handles yet."""
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.protocol = protocol
        super().__init__(
            f"{_provider_label(provider_name, provider_id)} uses protocol "
            f"{str(protocol)!r}, which is not "
            f"supported by the built-in adapter factory (supported protocols are "
            f"'openai_chat' and 'openai_responses')."
        )


class ModelArgumentConflictError(ConfigError):
    """A CallVariant's arguments already contain "model"; the router always injects it."""

    def __init__(self, protocol: ApiProtocol, operation: str) -> None:
        self.protocol = protocol
        self.operation = operation
        super().__init__(
            f"CallVariant for protocol {str(protocol)!r} operation {operation!r} already "
            f"has a 'model' key in its arguments -- the router always supplies the "
            f"provider's configured model; remove 'model' from arguments."
        )


class DuplicateCallVariantProtocolError(ConfigError):
    """Two CallVariants in one invoke() call share the same protocol."""

    def __init__(self, protocol: ApiProtocol) -> None:
        self.protocol = protocol
        super().__init__(
            f"More than one CallVariant was supplied for protocol {str(protocol)!r} in a "
            f"single invoke() call; each protocol must appear at most once."
        )


class MixedCallTypeError(ConfigError):
    """CallVariants in one invocation declare conflicting response contracts."""

    def __init__(self, conflicts: list[tuple[ApiProtocol, CallType]]) -> None:
        self.conflicts = conflicts
        detail = ", ".join(
            f"{protocol.value}={call_type.value}" for protocol, call_type in conflicts
        )
        super().__init__(
            "All CallVariants in one invoke() call must declare the same call_type; "
            f"received {detail}."
        )


class ProviderSDKNotInstalledError(ConfigError):
    """The provider SDK a protocol's adapter needs is not installed."""

    def __init__(
        self,
        provider_id: str,
        provider_name: str,
        package: str,
        original: BaseException | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.package = package
        self.original = original
        super().__init__(
            f"{_provider_label(provider_name, provider_id)} requires the {package!r} package, "
            "which is not "
            f'installed: pip install "llm-provider-router[{package}]".'
        )


class NoProvidersConfiguredError(ConfigError):
    """No usable provider was available when routing a request."""


class NoEligibleProvidersError(NygenRouterError):
    """Every configured provider was filtered out before any call was made.

    Per the transparency principle, the message enumerates each excluded
    provider with its own specific reason rather than a single blended
    summary; the structured results stay available on ``.exclusions``.
    """

    def __init__(self, exclusions: list[EligibilityResult]) -> None:
        self.exclusions = exclusions
        detail = "; ".join(
            f'{result.provider_name} (id="{result.provider_id}"): {result.detail}'
            for result in exclusions
        )
        super().__init__(f"No eligible providers for this request: {detail}.")


class RouterExhaustedError(NygenRouterError):
    """Every eligible provider that was actually tried failed.

    Per the transparency principle, the message enumerates each attempted
    provider with its own real, distinct failure rather than a single blended
    summary; the structured attempts (each with its unwrapped error object)
    stay available on ``.attempts``.
    """

    def __init__(self, attempts: list[ProviderAttempt]) -> None:
        self.attempts = attempts
        detail = "; ".join(
            f'{attempt.provider_name} (id="{attempt.provider_id}"): {attempt.error}'
            for attempt in attempts
        )
        super().__init__(f"All attempted providers failed: {detail}.")


class ProviderError(NygenRouterError):
    """A call to a provider failed at the transport level.

    The original exception (e.g. ``httpx.ConnectError``) is preserved both as
    ``__cause__`` (via ``raise ... from``) and on ``.original``.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        provider_name: str,
        model: str,
        original: BaseException | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.model = model
        self.original = original
        super().__init__(message)


class ProviderTimeoutError(ProviderError):
    """A provider request exceeded its configured timeout."""


class ProviderConnectionError(ProviderError):
    """A provider request could not establish a connection."""


class UnsupportedOperationError(ProviderError):
    """A CallVariant's ``operation`` does not resolve on the provider's SDK client."""


class InvalidOperationArgumentsError(ProviderError):
    """A CallVariant's ``arguments`` do not match its resolved operation's signature."""


class ProviderStreamInterruptedError(ProviderError):
    """A stream ended empty or without the provider ever marking it complete.

    Synthesized by the router, not raised by any SDK: an empty or silently
    truncated stream has no HTTP status and no transport failure of its own, so
    there is no original exception to chain here.
    """


class ProviderResponsesError(ProviderError):
    """A Responses API terminal event or response declared generation failed."""

    def __init__(
        self,
        *,
        provider_id: str,
        provider_name: str,
        model: str,
        message: str,
        error_code: str | None,
        param: str | None = None,
        event: Any = None,
        response: Any = None,
    ) -> None:
        self.message = message
        self.error_code = error_code
        self.param = param
        self.event = event
        self.response = response
        tags = "/".join(tag for tag in (error_code, param) if tag)
        tag_suffix = f" [{tags}]" if tags else ""
        super().__init__(
            f"{_provider_label(provider_name, provider_id)} Responses API failed for model "
            f"{model!r}: "
            f"{message}{tag_suffix}",
            provider_id=provider_id,
            provider_name=provider_name,
            model=model,
        )


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
        provider_id: str,
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
            f"{_provider_label(provider_name, provider_id)} returned {status} for model {model!r}: "
            f"{detail}{tag_suffix}",
            provider_id=provider_id,
            provider_name=provider_name,
            model=model,
            original=original,
        )


def _http_reason(status_code: int) -> str:
    """Map a status code to its standard reason phrase, or "" if unknown."""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return ""


class ErrorCategory(StrEnum):
    """Public failure classification used by fallback and retry decisions.

    Values describe what happened; they do not claim a request is safe to
    replay. ``RetryContext`` exposes the same enum to custom retry policies.
    """

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    SERVER_ERROR = "server_error"
    CONNECTION = "connection"
    STREAM_INTERRUPTED = "stream_interrupted"
    BAD_REQUEST = "bad_request"
    INVALID_OPERATION = "invalid_operation"
    UNKNOWN = "unknown"


def categorize_error(exc: Exception) -> ErrorCategory:
    """Classify a provider failure so the fallback loop can decide what to do.

    Only 400/422 count as BAD_REQUEST (the request itself is malformed, so no
    provider will do better). Other 4xx like 404/413 are provider-specific --
    wrong base_url, model not hosted there, smaller payload limits -- and must
    not stop the run while valid providers remain. INVALID_OPERATION covers
    caller/config mistakes discovered before any provider request is even
    sent (a bad operation string, arguments that don't match it, or a missing
    provider SDK) -- every provider sharing that protocol would fail
    identically, so the run stops rather than burying the real cause.
    """
    invalid_operation_errors = (
        UnsupportedOperationError,
        InvalidOperationArgumentsError,
        ProviderSDKNotInstalledError,
    )
    if isinstance(exc, invalid_operation_errors):
        return ErrorCategory.INVALID_OPERATION
    if isinstance(exc, ProviderTimeoutError):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, ProviderConnectionError):
        return ErrorCategory.CONNECTION
    if isinstance(exc, ProviderStreamInterruptedError):
        return ErrorCategory.STREAM_INTERRUPTED
    if isinstance(exc, ProviderResponsesError):
        return _categorize_responses_code(exc.error_code)
    if isinstance(exc, ProviderHTTPError):
        status = exc.status_code
        if status == 429:
            return ErrorCategory.RATE_LIMIT
        if status in (401, 403):
            return ErrorCategory.AUTH
        if status == 408:
            return ErrorCategory.TIMEOUT
        if status >= 500:
            return ErrorCategory.SERVER_ERROR
        if status in (400, 422):
            return ErrorCategory.BAD_REQUEST
        return ErrorCategory.UNKNOWN
    return ErrorCategory.UNKNOWN


def _categorize_responses_code(code: str | None) -> ErrorCategory:
    """Classify only provider codes whose meaning is explicit and stable."""
    if code in {"rate_limit", "rate_limit_exceeded"}:
        return ErrorCategory.RATE_LIMIT
    if code in {"internal_server_error", "server_error"}:
        return ErrorCategory.SERVER_ERROR
    if code in {"request_timeout", "timeout", "vector_store_timeout"}:
        return ErrorCategory.TIMEOUT
    if code in {
        "empty_image_file",
        "failed_to_download_image",
        "image_file_not_found",
        "image_file_too_large",
        "image_parse_error",
        "image_too_large",
        "image_too_small",
        "invalid_base64_image",
        "invalid_image",
        "invalid_image_format",
        "invalid_image_mode",
        "invalid_image_url",
        "invalid_input",
        "invalid_prompt",
        "invalid_request",
        "unsupported_image_media_type",
    }:
        return ErrorCategory.BAD_REQUEST
    return ErrorCategory.UNKNOWN
