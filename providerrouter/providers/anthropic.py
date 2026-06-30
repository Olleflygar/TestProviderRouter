from __future__ import annotations

import time
from typing import Any

from providerrouter.exceptions import MissingProviderKey, ProviderError
from providerrouter.providers.base import BaseProvider
from providerrouter.result import RouterResult


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    supported_models = ["claude"]

    def call(self, model: str, messages: list[dict], **kwargs) -> RouterResult:
        if not self.api_key:
            raise MissingProviderKey(self.name, self.env_var or "ANTHROPIC_API_KEY")

        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "The 'anthropic' package is required for the anthropic provider. "
                "Install it with: pip install 'providerrouter[anthropic]'"
            ) from None

        system, anthropic_messages = _split_system_message(messages)
        started = time.perf_counter()
        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=model,
                system=system,
                messages=anthropic_messages,
                max_tokens=kwargs.pop("max_tokens", 1024),
                **kwargs,
            )
        except Exception as exc:
            raise ProviderError(self.name, model, exc) from exc

        return RouterResult(
            content=_extract_content(response),
            provider=self.name,
            model=model,
            raw=response,
            metadata={"latency_ms": _elapsed_ms(started), **_usage_metadata(response)},
        )

    async def acall(self, model: str, messages: list[dict], **kwargs) -> RouterResult:
        if not self.api_key:
            raise MissingProviderKey(self.name, self.env_var or "ANTHROPIC_API_KEY")

        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "The 'anthropic' package is required for the anthropic provider. "
                "Install it with: pip install 'providerrouter[anthropic]'"
            ) from None

        system, anthropic_messages = _split_system_message(messages)
        started = time.perf_counter()
        try:
            client = anthropic.AsyncAnthropic(api_key=self.api_key)
            response = await client.messages.create(
                model=model,
                system=system,
                messages=anthropic_messages,
                max_tokens=kwargs.pop("max_tokens", 1024),
                **kwargs,
            )
        except Exception as exc:
            raise ProviderError(self.name, model, exc) from exc

        return RouterResult(
            content=_extract_content(response),
            provider=self.name,
            model=model,
            raw=response,
            metadata={"latency_ms": _elapsed_ms(started), **_usage_metadata(response)},
        )


def _split_system_message(messages: list[dict]) -> tuple[str | None, list[dict]]:
    system_parts = []
    remaining = []
    for message in messages:
        if message.get("role") == "system":
            system_parts.append(str(message.get("content", "")))
        else:
            remaining.append(message)
    system = "\n".join(part for part in system_parts if part) or None
    return system, remaining


def _extract_content(response: Any) -> str:
    parts = getattr(response, "content", []) or []
    text_parts = []
    for part in parts:
        text = getattr(part, "text", None)
        if text is not None:
            text_parts.append(text)
    return "".join(text_parts)


def _usage_metadata(response: Any) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return {"usage": usage.model_dump()}
    return {"usage": usage}


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
