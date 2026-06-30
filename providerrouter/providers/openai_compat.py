from __future__ import annotations

import time
from typing import Any

from providerrouter.exceptions import MissingProviderKey, ProviderError
from providerrouter.providers.base import BaseProvider
from providerrouter.result import RouterResult


DEFAULT_API_BASES = {
    "openai": "https://api.openai.com/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "azure": None,
}

SUPPORTED_MODEL_FRAGMENTS = {
    "openai": ["gpt", "o1", "o3", "o4"],
    "together": [],
    "fireworks": [],
    "azure": ["gpt", "o1", "o3", "o4"],
}


class OpenAICompatibleProvider(BaseProvider):
    def __init__(
        self,
        name: str,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        env_var: str | None = None,
        config: dict | None = None,
    ):
        super().__init__(
            api_key=api_key,
            api_base=api_base or DEFAULT_API_BASES.get(name),
            env_var=env_var,
            config=config,
        )
        self.name = name
        self.supported_models = SUPPORTED_MODEL_FRAGMENTS.get(name, [])

    def call(self, model: str, messages: list[dict], **kwargs) -> RouterResult:
        if not self.api_key:
            raise MissingProviderKey(self.name, self.env_var or "API_KEY")

        try:
            import openai
        except ImportError:
            raise ImportError(
                "The 'openai' package is required for the openai/together/fireworks/azure "
                "providers. Install it with: pip install 'providerrouter[openai]'"
            ) from None

        started = time.perf_counter()
        try:
            client = openai.OpenAI(api_key=self.api_key, base_url=self.api_base)
            response = client.chat.completions.create(
                model=model,
                messages=messages,
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
            raise MissingProviderKey(self.name, self.env_var or "API_KEY")

        try:
            import openai
        except ImportError:
            raise ImportError(
                "The 'openai' package is required for the openai/together/fireworks/azure "
                "providers. Install it with: pip install 'providerrouter[openai]'"
            ) from None

        started = time.perf_counter()
        try:
            client = openai.AsyncOpenAI(api_key=self.api_key, base_url=self.api_base)
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
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


def _extract_content(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def _usage_metadata(response: Any) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return {"usage": usage.model_dump()}
    return {"usage": usage}


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
