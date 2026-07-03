from __future__ import annotations

from typing import Any, cast

import httpx

from nygen_router.config import ProviderConfig
from nygen_router.errors import ProviderError, ProviderHTTPError, ProviderResponseError
from nygen_router.types import RouterRequest, RouterResponse, TokenUsage


class OpenAICompatibleAdapter:
    def __init__(self, config: ProviderConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._transport = transport

    def invoke(self, request: RouterRequest) -> RouterResponse:
        payload: dict[str, object] = {
            "model": self.config.model,
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
                    self._endpoint(),
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.config.resolve_api_key()}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderError(self.config.name, str(exc)) from exc

        if response.status_code >= 400:
            raise ProviderHTTPError(
                self.config.name,
                response.status_code,
                _response_error_message(response),
            )

        raw = _decode_json(response, self.config.name)
        return RouterResponse(
            provider_name=self.config.name,
            model=self.config.model,
            text=_extract_text(raw, self.config.name),
            raw=raw,
            usage=_extract_usage(raw),
        )

    def _endpoint(self) -> str:
        if self.config.base_url is None:
            raise ProviderError(self.config.name, "base_url is required.")
        return f"{self.config.base_url.rstrip('/')}/chat/completions"


def _decode_json(response: httpx.Response, provider_name: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderResponseError(provider_name, "Response body is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise ProviderResponseError(provider_name, "Response JSON must be an object.")
    return cast(dict[str, object], payload)


def _extract_text(raw: dict[str, object], provider_name: str) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderResponseError(provider_name, "Response is missing choices.")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ProviderResponseError(provider_name, "Response choice must be an object.")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError(provider_name, "Response choice is missing message.")

    content = message.get("content")
    if not isinstance(content, str):
        raise ProviderResponseError(provider_name, "Response message is missing content.")

    return content


def _extract_usage(raw: dict[str, object]) -> TokenUsage | None:
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


def _response_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        message = payload.get("message")
        if isinstance(message, str):
            return message
    return response.text
