from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nygen_router.adapters.base import NormalizedStream
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
        # One SDK client per adapter, built on first use and kept so its pooled
        # HTTP connections survive across attempts instead of paying a fresh
        # TCP/TLS handshake per request.
        self._client: Any = None
        self._client_api_key: str | None = None

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        """Resolve operation on an openai client and call it with arguments, or raise."""
        name = self.config.name
        provider_id = self.config.provider_id
        model = self.config.model

        try:
            import openai
        except ModuleNotFoundError as exc:
            raise ProviderSDKNotInstalledError(provider_id, name, "openai", original=exc) from exc

        client = self._client_for(openai)

        target: Any = client
        try:
            for part in operation.split("."):
                target = getattr(target, part)
        except AttributeError as exc:
            raise UnsupportedOperationError(
                f"{_label(name, provider_id)} has no operation {operation!r} on its openai client "
                f"({type(exc).__name__}): {exc}",
                provider_id=provider_id,
                provider_name=name,
                model=model,
                original=exc,
            ) from exc

        try:
            response = target(**arguments)
        except TypeError as exc:
            # Dispatch-specific, so it stays here rather than in the shared
            # mapping: mid-stream a TypeError means the provider sent something
            # unusable, never that the caller's arguments were wrong.
            raise InvalidOperationArgumentsError(
                f"{_label(name, provider_id)} operation {operation!r} rejected the given "
                "arguments for "
                f"model {model!r} ({type(exc).__name__}): {exc}",
                provider_id=provider_id,
                provider_name=name,
                model=model,
                original=exc,
            ) from exc
        except Exception as exc:
            mapped = _map_sdk_exception(
                exc,
                provider_id=provider_id,
                provider_name=name,
                model=model,
                timeout_seconds=self.config.timeout_seconds,
            )
            if mapped is None:  # pragma: no cover
                # Unreachable through .create() today -- the SDK folds even an
                # unrelated transport exception into APIConnectionError -- but
                # what the mapping cannot classify is passed on exactly as it
                # arrived rather than re-wrapped into a guess.
                raise
            raise mapped from exc

        if isinstance(response, openai.Stream):
            return self._wrap_stream(response)
        return self._handle_response(response)

    def _client_for(self, openai_module: Any) -> Any:
        """Return the cached SDK client, rebuilding it only on a changed resolved key.

        Re-resolving per attempt keeps the pre-cache contract: a key corrected
        mid-run takes effect on the next call, so reset_health() still recovers
        an auth-benched provider without a process restart.
        """
        api_key = self.config.resolve_api_key()
        if self._client is None or api_key != self._client_api_key:
            self._client = openai_module.OpenAI(
                api_key=api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout_seconds,
                # SDK retries stay disabled so optional router-controlled same-provider
                # retries and cross-provider fallback remain observable as physical attempts.
                max_retries=0,
                http_client=self._http_client,
            )
            self._client_api_key = api_key
        return self._client

    def _wrap_stream(self, stream: Any) -> NormalizedStream:
        """Wrap the SDK stream shape this adapter understands."""
        return OpenAIChatStream(
            stream,
            provider_id=self.config.provider_id,
            provider_name=self.config.name,
            model=self.config.model,
            timeout_seconds=self.config.timeout_seconds,
        )

    def _handle_response(self, response: Any) -> Any:
        """Observe a non-streaming response without changing its identity."""
        return response


class OpenAIChatStream(NormalizedStream):
    """Wraps an ``openai.Stream`` so the router can observe it, chunk for chunk.

    Chunks are handed on exactly as the SDK produced them -- nothing buffered,
    accumulated, reordered, or invented -- so a consumer's loop is
    indistinguishable from iterating the SDK stream directly. The only
    per-chunk work is reading ``finish_reason`` and pocketing the usage object
    the ``include_usage`` final chunk carries.
    """

    def __init__(
        self,
        stream: Any,
        *,
        provider_id: str,
        provider_name: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._stream = stream
        self._provider_id = provider_id
        self._provider_name = provider_name
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._completed = False
        self._recognized = False
        self._usage: Any = None

    def __next__(self) -> Any:
        try:
            chunk = next(self._stream)
        except StopIteration:
            raise
        except Exception as exc:
            raise self._as_router_error(exc) from exc
        self._observe(chunk)
        return chunk

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def recognized(self) -> bool:
        return self._recognized

    @property
    def usage(self) -> Any:
        return self._usage

    def close(self) -> None:
        self._stream.close()

    def _observe(self, chunk: Any) -> None:
        """Read a chunk's completion marker and usage without altering it."""
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self._usage = usage
        choices = getattr(chunk, "choices", None)
        if not choices:
            # Two different chunks land here and neither says anything about
            # completion: the include_usage final chunk, whose choices list is
            # empty and which arrives after the finish_reason chunk, and a
            # shape this wrapper does not recognize at all.
            return
        self._recognized = True
        if any(getattr(choice, "finish_reason", None) is not None for choice in choices):
            self._completed = True

    def _as_router_error(self, exc: Exception) -> ProviderError:
        """Map anything escaping mid-iteration onto the router's error hierarchy.

        The ABC's contract is that only router errors leave ``__next__``, and
        iteration raises a wider set than ``.create()`` does -- raw httpx
        transport errors, and a JSONDecodeError from a malformed SSE payload --
        so an unrecognized type becomes a plain ProviderError rather than
        reaching the consumer as an SDK-shaped surprise.
        """
        return _map_stream_exception(
            exc,
            provider_id=self._provider_id,
            provider_name=self._provider_name,
            model=self._model,
            timeout_seconds=self._timeout_seconds,
        )


def _map_stream_exception(
    exc: Exception,
    *,
    provider_id: str,
    provider_name: str,
    model: str,
    timeout_seconds: float,
) -> ProviderError:
    """Map any exception escaping SDK stream iteration to a router error."""
    mapped = _map_sdk_exception(
        exc,
        provider_id=provider_id,
        provider_name=provider_name,
        model=model,
        timeout_seconds=timeout_seconds,
    )
    if mapped is not None:
        return mapped
    return ProviderError(
        f"{_label(provider_name, provider_id)} stream failed for model {model!r} "
        f"({type(exc).__name__}): {exc}",
        provider_id=provider_id,
        provider_name=provider_name,
        model=model,
        original=exc,
    )


def _map_sdk_exception(
    exc: Exception,
    *,
    provider_id: str,
    provider_name: str,
    model: str,
    timeout_seconds: float,
) -> ProviderError | None:
    """Map one SDK or transport exception onto the router's error hierarchy.

    The single copy of this mapping, shared by ``invoke()`` and the stream
    wrapper. The httpx branches exist because the SDK only folds transport
    failures into its own exception types around ``.create()``: during SSE
    iteration a read timeout or a dropped connection escapes as the raw httpx
    exception. Returns None for anything unrecognized, so a caller can decide
    whether to re-raise it untouched or supply its own fallback.
    """
    import httpx
    import openai

    if isinstance(exc, openai.APITimeoutError | httpx.TimeoutException):
        return ProviderTimeoutError(
            f"{_label(provider_name, provider_id)} timed out after {timeout_seconds}s for model "
            f"{model!r} ({type(exc).__name__}): {exc}",
            provider_id=provider_id,
            provider_name=provider_name,
            model=model,
            original=exc,
        )
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError):
        return ProviderConnectionError(
            f"{_label(provider_name, provider_id)} could not connect for model {model!r} "
            f"({type(exc).__name__}): {exc}",
            provider_id=provider_id,
            provider_name=provider_name,
            model=model,
            original=exc,
        )
    if isinstance(exc, openai.APIStatusError):
        return ProviderHTTPError(
            provider_id=provider_id,
            provider_name=provider_name,
            model=model,
            status_code=exc.status_code,
            message=_verbatim_message(exc.body, fallback=exc.message),
            error_type=exc.type,
            error_code=exc.code,
            body=exc.body,
            response=exc.response,
            original=exc,
        )
    if isinstance(exc, openai.OpenAIError):
        return ProviderError(
            f"{_label(provider_name, provider_id)} request failed for model {model!r} "
            f"({type(exc).__name__}): {exc}",
            provider_id=provider_id,
            provider_name=provider_name,
            model=model,
            original=exc,
        )
    return None


def _label(provider_name: str, provider_id: str) -> str:
    return f'Provider "{provider_name}" (id="{provider_id}")'


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
