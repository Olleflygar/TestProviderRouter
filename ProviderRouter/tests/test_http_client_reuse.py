"""HTTP connection reuse: built-in adapters keep one SDK client per provider.

The client -- and therefore its pooled HTTP connections -- survives across
invocations instead of being rebuilt per physical attempt. The one rebuild
trigger is a changed resolved API key, preserving the previous per-attempt
behavior of a corrected key taking effect immediately. The router reuses only
default-factory adapters; a custom ``adapter_factory`` keeps its existing
call-per-attempt behavior.

The cached-client assertions read the adapter's private cache directly: with an
injected ``http_client`` the wire traffic is identical whether the SDK client
was rebuilt or reused, so object identity is the only honest observable.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ProviderConfig,
    ProviderRouter,
)
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter


def _provider(provider_id: str, **overrides: Any) -> ProviderConfig:
    values: dict[str, Any] = {
        "provider_id": provider_id,
        "name": provider_id,
        "protocol": ApiProtocol.OPENAI_CHAT,
        "model": "model-a",
        "base_url": "https://api.example.com/v1/",
        "api_key": "secret",
    }
    values.update(overrides)
    return ProviderConfig(**values)


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _arguments() -> dict[str, object]:
    return {"model": "model-a", "messages": [{"role": "user", "content": "Hello"}]}


def _completion_body() -> dict[str, object]:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "model-a",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }
        ],
    }


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_completion_body())


def test_adapter_reuses_one_sdk_client_across_invocations() -> None:
    adapter = OpenAICompatibleAdapter(_provider("provider-a"), http_client=_client(_ok_handler))

    adapter.invoke("chat.completions.create", _arguments())
    first_client = adapter._client
    adapter.invoke("chat.completions.create", _arguments())

    assert first_client is not None
    assert adapter._client is first_client


def test_adapter_rebuilds_client_only_when_resolved_api_key_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_authorization.append(request.headers["Authorization"])
        return httpx.Response(200, json=_completion_body())

    monkeypatch.setenv("REUSE_TEST_API_KEY", "key-one")
    adapter = OpenAICompatibleAdapter(
        _provider("provider-a", api_key=None, api_key_env="REUSE_TEST_API_KEY"),
        http_client=_client(handler),
    )

    adapter.invoke("chat.completions.create", _arguments())
    unchanged_client = adapter._client
    adapter.invoke("chat.completions.create", _arguments())
    assert adapter._client is unchanged_client

    monkeypatch.setenv("REUSE_TEST_API_KEY", "key-two")
    adapter.invoke("chat.completions.create", _arguments())

    assert sent_authorization == ["Bearer key-one", "Bearer key-one", "Bearer key-two"]
    assert adapter._client is not unchanged_client


def test_default_adapter_factory_reuses_one_adapter_per_provider() -> None:
    provider_a = _provider("provider-a")
    provider_b = _provider("provider-b")
    router = ProviderRouter([provider_a, provider_b], metrics_scope="test", metrics_store=None)

    first = router._adapter_for(provider_a)

    assert router._adapter_for(provider_a) is first
    assert router._adapter_for(provider_b) is not first


def test_custom_adapter_factory_is_still_called_per_invocation() -> None:
    factory_calls: list[str] = []
    response = object()

    class _Adapter:
        def invoke(self, operation: str, arguments: dict[str, object]) -> object:
            return response

    def factory(provider: ProviderConfig) -> _Adapter:
        factory_calls.append(provider.provider_id)
        return _Adapter()

    router = ProviderRouter(
        [_provider("provider-a")],
        metrics_scope="test",
        metrics_store=None,
        adapter_factory=factory,
    )
    call = CallVariant(
        protocol=ApiProtocol.OPENAI_CHAT,
        operation="chat.completions.create",
        call_type=CallType.REGULAR,
        arguments={"messages": []},
    )

    assert router.invoke([call]) is response
    assert router.invoke([call]) is response

    assert factory_calls == ["provider-a", "provider-a"]
