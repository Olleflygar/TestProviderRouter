from __future__ import annotations

import os
import time
from typing import Any, Callable

from providerrouter.exceptions import NoProvidersAvailable, RouterError
from providerrouter.providers import AnthropicProvider, BaseProvider, OpenAICompatibleProvider
from providerrouter.result import RouterResult
from providerrouter.state import BaseState, InMemoryState


PROVIDER_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "azure": "AZURE_API_KEY",
}

OPENAI_COMPATIBLE_PROVIDERS = {"openai", "together", "fireworks", "azure"}


class ProviderRouter:
    def __init__(
        self,
        preferred_model: str,
        providers: dict[str, dict[str, Any]] | None = None,
        state: BaseState | None = None,
    ):
        self.preferred_model = preferred_model
        self.state = state or InMemoryState()
        self._callbacks: list[Callable[[dict], None]] = []
        self._last_decision: dict | None = None
        self._provider_names, self._providers = self._build_providers(providers)
        self.providers = _ProvidersNamespace(self)
        self.audit = _AuditNamespace(self)

    def invoke(self, messages: list[dict], **kwargs) -> RouterResult:
        provider = self._next_provider()
        started = time.perf_counter()
        try:
            result = provider.call(self.preferred_model, messages, **kwargs)
        except Exception as exc:
            self._record_decision(
                provider=provider.name,
                latency_ms=_elapsed_ms(started),
                success=False,
                error=str(exc),
            )
            raise

        self._record_decision(
            provider=result.provider,
            latency_ms=int(result.metadata.get("latency_ms", _elapsed_ms(started))),
            success=True,
            error=None,
        )
        return result

    async def ainvoke(self, messages: list[dict], **kwargs) -> RouterResult:
        provider = self._next_provider()
        started = time.perf_counter()
        try:
            result = await provider.acall(self.preferred_model, messages, **kwargs)
        except Exception as exc:
            self._record_decision(
                provider=provider.name,
                latency_ms=_elapsed_ms(started),
                success=False,
                error=str(exc),
            )
            raise

        self._record_decision(
            provider=result.provider,
            latency_ms=int(result.metadata.get("latency_ms", _elapsed_ms(started))),
            success=True,
            error=None,
        )
        return result

    def __call__(self, messages: list[dict], **kwargs) -> RouterResult:
        return self.invoke(messages, **kwargs)

    def on_decision(self, callback: Callable[[dict], None]) -> None:
        self._callbacks.append(callback)

    def _next_provider(self) -> BaseProvider:
        if not self._provider_names:
            raise NoProvidersAvailable(
                "No providers are configured. Pass providers={...} or set one of: "
                + ", ".join(PROVIDER_ENV_VARS.values())
            )
        provider_name = self.state.get_next_provider(self._provider_names)
        return self._providers[provider_name]

    def _build_providers(
        self,
        providers: dict[str, dict[str, Any]] | None,
    ) -> tuple[list[str], dict[str, BaseProvider]]:
        if providers is None:
            provider_configs = {
                name: {}
                for name, env_var in PROVIDER_ENV_VARS.items()
                if os.environ.get(env_var)
            }
        else:
            provider_configs = dict(providers)

        provider_names = list(provider_configs.keys())
        provider_objects = {
            name: self._build_provider(name, config or {})
            for name, config in provider_configs.items()
        }
        return provider_names, provider_objects

    def _build_provider(self, name: str, config: dict[str, Any]) -> BaseProvider:
        normalized = name.lower()
        env_var = PROVIDER_ENV_VARS.get(normalized)
        api_key = config.get("api_key") or (os.environ.get(env_var) if env_var else None)
        api_base = config.get("api_base")

        if normalized in OPENAI_COMPATIBLE_PROVIDERS:
            return OpenAICompatibleProvider(
                normalized,
                api_key=api_key,
                api_base=api_base,
                env_var=env_var,
                config=config,
            )
        if normalized == "anthropic":
            return AnthropicProvider(
                api_key=api_key,
                api_base=api_base,
                env_var=env_var,
                config=config,
            )
        raise RouterError(f"Unsupported provider '{name}'")

    def _record_decision(
        self,
        *,
        provider: str,
        latency_ms: int,
        success: bool,
        error: str | None,
    ) -> None:
        decision = {
            "provider": provider,
            "model": self.preferred_model,
            "latency_ms": latency_ms,
            "success": success,
            "error": error,
        }
        self._last_decision = decision
        for callback in self._callbacks:
            callback(decision)


class _ProvidersNamespace:
    def __init__(self, router: ProviderRouter):
        self._router = router

    def list(self) -> list[str]:
        return list(self._router._provider_names)

    def current(self) -> str | None:
        if not self._router._provider_names:
            return None
        index = getattr(self._router.state, "_index", 0)
        return self._router._provider_names[index % len(self._router._provider_names)]


class _AuditNamespace:
    def __init__(self, router: ProviderRouter):
        self._router = router

    def last_decision(self) -> dict | None:
        if self._router._last_decision is None:
            return None
        return dict(self._router._last_decision)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
