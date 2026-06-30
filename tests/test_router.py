from __future__ import annotations

import pytest

from providerrouter import MissingProviderKey, NoProvidersAvailable, ProviderRouter, RouterResult
from providerrouter.providers import BaseProvider


MESSAGES = [{"role": "user", "content": "hello"}]


class MockProvider(BaseProvider):
    supported_models = []

    def __init__(self, name: str):
        super().__init__(api_key="test")
        self.name = name
        self.calls = 0

    def call(self, model: str, messages: list[dict], **kwargs) -> RouterResult:
        self.calls += 1
        return RouterResult(
            content=f"{self.name}:{model}",
            provider=self.name,
            model=model,
            raw={"messages": messages, "kwargs": kwargs},
            metadata={"latency_ms": 1},
        )

    async def acall(self, model: str, messages: list[dict], **kwargs) -> RouterResult:
        return self.call(model, messages, **kwargs)


def test_provider_router_rotates_over_three_calls():
    router = ProviderRouter(
        preferred_model="gpt-4o-mini",
        providers={"openai": {"api_key": "a"}, "together": {"api_key": "b"}},
    )
    router._providers = {
        "openai": MockProvider("openai"),
        "together": MockProvider("together"),
    }

    assert router.invoke(MESSAGES).provider == "openai"
    assert router.invoke(MESSAGES).provider == "together"
    assert router.invoke(MESSAGES).provider == "openai"


def test_no_providers_available_raises_on_invoke():
    router = ProviderRouter(preferred_model="gpt-4o-mini", providers={})

    with pytest.raises(NoProvidersAvailable):
        router.invoke(MESSAGES)


def test_missing_key_raises_lazily_not_at_construction(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    router = ProviderRouter(preferred_model="gpt-4o-mini", providers={"openai": {}})

    with pytest.raises(MissingProviderKey) as exc_info:
        router.invoke(MESSAGES)
    assert exc_info.value.provider == "openai"
    assert exc_info.value.env_var == "OPENAI_API_KEY"


def test_result_content_and_output_are_aliases():
    router = ProviderRouter(
        preferred_model="gpt-4o-mini",
        providers={"openai": {"api_key": "a"}},
    )
    router._providers = {"openai": MockProvider("openai")}

    response = router.invoke(MESSAGES)

    assert response.content == "openai:gpt-4o-mini"
    assert response.output == response.content
    assert str(response) == response.content


def test_call_alias_matches_invoke():
    router = ProviderRouter(
        preferred_model="gpt-4o-mini",
        providers={"openai": {"api_key": "a"}},
    )
    router._providers = {"openai": MockProvider("openai")}

    response = router(MESSAGES)

    assert response.provider == "openai"
    assert response.content == "openai:gpt-4o-mini"


def test_audit_last_decision_after_successful_call():
    router = ProviderRouter(
        preferred_model="gpt-4o-mini",
        providers={"openai": {"api_key": "a"}},
    )
    router._providers = {"openai": MockProvider("openai")}

    router.invoke(MESSAGES)
    decision = router.audit.last_decision()

    assert decision == {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "latency_ms": 1,
        "success": True,
        "error": None,
    }


def test_provider_namespace_list_and_current():
    router = ProviderRouter(
        preferred_model="gpt-4o-mini",
        providers={"openai": {"api_key": "a"}, "together": {"api_key": "b"}},
    )

    assert router.providers.list() == ["openai", "together"]
    assert router.providers.current() == "openai"
