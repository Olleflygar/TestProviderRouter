from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nygen_router.config import ProviderConfig
from nygen_router.errors import (
    InvalidOperationArgumentsError,
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderSDKNotInstalledError,
    ProviderTimeoutError,
    UnsupportedOperationError,
)

if TYPE_CHECKING:
    import httpx


class OpenAICompatibleAdapter:
    """Dispatches a prepared (operation, arguments) pair to the openai SDK.

    Lazily imports ``openai`` so the core package stays importable without it
    installed. Deliberately thin: the router decides which CallVariant
    applies and injects the model before calling in here -- this adapter's
    only job is dynamic dispatch and mapping the SDK's own exceptions onto
    the router's error hierarchy, never re-shaping the request itself.
    """

    def __init__(self, config: ProviderConfig, http_client: httpx.Client | None = None) -> None:
        self.config = config
        self._http_client = http_client

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        """Resolve operation on an openai client and call it with arguments, or raise."""
        name = self.config.name
        model = self.config.model

        try:
            import openai
        except ModuleNotFoundError as exc:
            raise ProviderSDKNotInstalledError(name, "openai", original=exc) from exc

        client = openai.OpenAI(
            api_key=self.config.resolve_api_key(),
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
            max_retries=0,  # the router's own cross-provider fallback is the sole retry path
            http_client=self._http_client,
        )

        target: Any = client
        try:
            for part in operation.split("."):
                target = getattr(target, part)
        except AttributeError as exc:
            raise UnsupportedOperationError(
                f"Provider {name!r} has no operation {operation!r} on its openai client "
                f"({type(exc).__name__}): {exc}",
                provider_name=name,
                model=model,
                original=exc,
            ) from exc

        try:
            return target(**arguments)
        except TypeError as exc:
            raise InvalidOperationArgumentsError(
                f"Provider {name!r} operation {operation!r} rejected the given arguments for "
                f"model {model!r} ({type(exc).__name__}): {exc}",
                provider_name=name,
                model=model,
                original=exc,
            ) from exc
        except openai.APITimeoutError as exc:
            raise ProviderTimeoutError(
                f"Provider {name!r} timed out after {self.config.timeout_seconds}s for model "
                f"{model!r} ({type(exc).__name__}): {exc}",
                provider_name=name,
                model=model,
                original=exc,
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderConnectionError(
                f"Provider {name!r} could not connect for model {model!r} "
                f"({type(exc).__name__}): {exc}",
                provider_name=name,
                model=model,
                original=exc,
            ) from exc
        except openai.APIStatusError as exc:
            raise ProviderHTTPError(
                provider_name=name,
                model=model,
                status_code=exc.status_code,
                message=_verbatim_message(exc.body, fallback=exc.message),
                error_type=exc.type,
                error_code=exc.code,
                body=exc.body,
                response=exc.response,
                original=exc,
            ) from exc
        except openai.OpenAIError as exc:  # pragma: no cover
            # Defensive: every realistic SDK failure through .create() is already caught
            # by a more specific branch above; kept for a future SDK version's new type.
            raise ProviderError(
                f"Provider {name!r} request failed for model {model!r} "
                f"({type(exc).__name__}): {exc}",
                provider_name=name,
                model=model,
                original=exc,
            ) from exc


def _verbatim_message(body: object, *, fallback: str) -> str:
    """Pull the provider's own error text out of the SDK's parsed body.

    ``APIStatusError.message`` is a summary the SDK synthesizes itself
    ("Error code: 404 - {...}"), not the provider's text -- the real message
    lives in ``body`` (already unwrapped from any ``{"error": {...}}``
    envelope by the SDK). Falls back to the synthesized summary if the body
    isn't shaped as expected, so a message is always available.
    """
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str):
            return message
    return fallback
