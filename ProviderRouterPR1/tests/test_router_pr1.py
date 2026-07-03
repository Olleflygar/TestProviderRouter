from __future__ import annotations

import pytest

from nygen_router import (
    ApiProtocol,
    ChatMessage,
    ProviderConfig,
    ProviderRouter,
    RouterRequest,
    RouterResponse,
)
from nygen_router.errors import (
    CapabilityError,
    NoProvidersConfiguredError,
    UnsupportedProtocolError,
)


def _openai_config(name: str = "provider_a", *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
        enabled=enabled,
    )


def test_router_raises_no_providers_configured_with_no_providers() -> None:
    router = ProviderRouter(providers=[])

    with pytest.raises(NoProvidersConfiguredError):
        router.invoke("Hello")


def test_router_invokes_first_enabled_openai_compatible_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called_with: dict[str, str] = {}

    class FakeAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, request: RouterRequest) -> RouterResponse:
            called_with["provider_name"] = self.config.name
            return RouterResponse(
                provider_name=self.config.name,
                model=self.config.model,
                text=request.messages[0].content,
            )

    monkeypatch.setattr("nygen_router.router.OpenAICompatibleAdapter", FakeAdapter)
    router = ProviderRouter(
        providers=[
            _openai_config("provider_a", enabled=False),
            _openai_config("provider_b", enabled=True),
        ]
    )

    response = router.invoke("Hello")

    assert called_with["provider_name"] == "provider_b"
    assert response.provider_name == "provider_b"


def test_router_normalizes_string_input_into_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, request: RouterRequest) -> RouterResponse:
            return RouterResponse(
                provider_name=self.config.name,
                model=self.config.model,
                text=f"{request.messages[0].role}:{request.messages[0].content}",
            )

    monkeypatch.setattr("nygen_router.router.OpenAICompatibleAdapter", FakeAdapter)
    router = ProviderRouter(providers=[_openai_config()])

    response = router.invoke("Hello")

    assert response.text == "user:Hello"


def test_router_raises_unsupported_protocol_for_unimplemented_protocol() -> None:
    router = ProviderRouter(
        providers=[
            ProviderConfig(
                name="anthropic",
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                model="claude-model",
                api_key="secret",
            )
        ]
    )

    with pytest.raises(UnsupportedProtocolError):
        router.invoke("Hello")


def test_router_rejects_tool_request_if_provider_does_not_support_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, request: RouterRequest) -> RouterResponse:
            raise AssertionError("adapter should not be called")

    monkeypatch.setattr("nygen_router.router.OpenAICompatibleAdapter", FakeAdapter)
    router = ProviderRouter(providers=[_openai_config()])
    request = RouterRequest(
        messages=[ChatMessage(role="user", content="Use a tool")],
        requires_tools=True,
    )

    with pytest.raises(CapabilityError) as exc_info:
        router.invoke(request)

    assert exc_info.value.provider_name == "provider_a"
    assert "tool calls" in str(exc_info.value)
