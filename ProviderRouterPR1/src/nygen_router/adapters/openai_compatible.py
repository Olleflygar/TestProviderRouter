from __future__ import annotations

from typing import Any, NoReturn, cast

import httpx

from nygen_router.config import ProviderConfig
from nygen_router.errors import (
    ConfigError,
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from nygen_router.types import RouterRequest, RouterResponse, TokenUsage


class OpenAICompatibleAdapter:
    def __init__(self, config: ProviderConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._transport = transport

    def invoke(self, request: RouterRequest) -> RouterResponse:
        """POST the request to /chat/completions and normalize the response or raise."""
        name = self.config.name
        model = self.config.model

        # Resolve the key first so a missing key surfaces as a configuration
        # error (MissingApiKeyError), never as a transport failure.
        api_key = self.config.resolve_api_key()
        url = self._endpoint()

        payload: dict[str, object] = {
            "model": model,
            "messages": [message.model_dump() for message in request.messages],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        try:
            with httpx.Client(
                timeout=self.config.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Provider {name!r} timed out after {self.config.timeout_seconds}s for model "
                f"{model!r} ({type(exc).__name__}): {exc}",
                provider_name=name,
                model=model,
                original=exc,
            ) from exc
        except httpx.ConnectError as exc:
            raise ProviderConnectionError(
                f"Provider {name!r} could not connect for model {model!r} "
                f"({type(exc).__name__}): {exc}",
                provider_name=name,
                model=model,
                original=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Provider {name!r} request failed for model {model!r} "
                f"({type(exc).__name__}): {exc}",
                provider_name=name,
                model=model,
                original=exc,
            ) from exc

        if response.status_code >= 400:
            _raise_http_error(self.config, response)

        raw = _decode_json(response, name, model)
        return RouterResponse(
            provider_name=name,
            model=model,
            text=_extract_text(raw, name, model),
            raw=raw,
            usage=_extract_usage(raw),
        )

    def _endpoint(self) -> str:
        """Build the chat/completions URL, or raise if base_url is missing."""
        if self.config.base_url is None:
            raise ConfigError(f"Provider {self.config.name!r} has no base_url configured.")
        return f"{self.config.base_url.rstrip('/')}/chat/completions"


def _raise_http_error(config: ProviderConfig, response: httpx.Response) -> NoReturn:
    """Turn a non-2xx response into a ProviderHTTPError, chained from httpx's own error."""
    message, error_type, error_code, body = _parse_provider_error(response)
    try:
        # Produces a standard httpx.HTTPStatusError we chain from, so the
        # familiar httpx error (with .request/.response) stays reachable.
        response.raise_for_status()
    except httpx.HTTPStatusError as status_error:
        raise ProviderHTTPError(
            provider_name=config.name,
            model=config.model,
            status_code=response.status_code,
            message=message,
            error_type=error_type,
            error_code=error_code,
            body=body,
            response=response,
            original=status_error,
        ) from status_error
    raise ProviderHTTPError(  # pragma: no cover - status >= 400 always raises above
        provider_name=config.name,
        model=config.model,
        status_code=response.status_code,
        message=message,
        error_type=error_type,
        error_code=error_code,
        body=body,
        response=response,
    )


def _parse_provider_error(
    response: httpx.Response,
) -> tuple[str, str | None, str | None, Any]:
    """Extract the provider's verbatim error message and structured fields.

    Handles the OpenAI-style ``{"error": {"message", "type", "code"}}`` shape,
    a top-level ``{"message": ...}``, and falls back to the raw response text.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text, None, None, None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            error_type = error.get("type")
            error_code = error.get("code")
            return (
                message if isinstance(message, str) else response.text,
                error_type if isinstance(error_type, str) else None,
                str(error_code) if error_code is not None else None,
                payload,
            )
        message = payload.get("message")
        if isinstance(message, str):
            return message, None, None, payload
    return response.text, None, None, payload


def _decode_json(response: httpx.Response, provider_name: str, model: str) -> dict[str, object]:
    """Parse the body as a JSON object, or raise ProviderResponseError."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderResponseError(
            provider_name,
            model,
            "response body is not valid JSON",
            body=response.text,
            response=response,
        ) from exc

    if not isinstance(payload, dict):
        raise ProviderResponseError(
            provider_name,
            model,
            "response JSON is not an object",
            body=payload,
            response=response,
        )
    return cast(dict[str, object], payload)


def _extract_text(raw: dict[str, object], provider_name: str, model: str) -> str:
    """Pull the assistant's message content out of the choices[0] shape."""
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderResponseError(provider_name, model, "response is missing 'choices'", body=raw)

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ProviderResponseError(
            provider_name, model, "response choice is not an object", body=raw
        )

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError(
            provider_name, model, "response choice is missing 'message'", body=raw
        )

    content = message.get("content")
    if not isinstance(content, str):
        raise ProviderResponseError(
            provider_name, model, "response message is missing 'content'", body=raw
        )

    return content


def _extract_usage(raw: dict[str, object]) -> TokenUsage | None:
    """Read token usage from the response if the provider included it."""
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None

    return TokenUsage(
        input_tokens=_optional_int(usage.get("prompt_tokens")),
        output_tokens=_optional_int(usage.get("completion_tokens")),
        total_tokens=_optional_int(usage.get("total_tokens")),
    )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None
