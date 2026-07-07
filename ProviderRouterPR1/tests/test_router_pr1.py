from __future__ import annotations

import pytest

from nygen_router import (
    ApiProtocol,
    ChatMessage,
    FilterReason,
    ProviderConfig,
    ProviderRouter,
    RouterRequest,
    RouterResponse,
)
from nygen_router.errors import (
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
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


def test_router_invokes_first_enabled_openai_compatible_provider() -> None:
    """Inject a fake adapter (no real HTTP) via adapter_factory to check provider selection only."""
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

    router = ProviderRouter(
        providers=[
            _openai_config("provider_a", enabled=False),
            _openai_config("provider_b", enabled=True),
        ],
        adapter_factory=FakeAdapter,
    )

    response = router.invoke("Hello")

    assert called_with["provider_name"] == "provider_b"
    assert response.provider_name == "provider_b"


def test_router_normalizes_string_input_into_user_message() -> None:
    class FakeAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, request: RouterRequest) -> RouterResponse:
            return RouterResponse(
                provider_name=self.config.name,
                model=self.config.model,
                text=f"{request.messages[0].role}:{request.messages[0].content}",
            )

    router = ProviderRouter(providers=[_openai_config()], adapter_factory=FakeAdapter)

    response = router.invoke("Hello")

    assert response.text == "user:Hello"


def test_router_excludes_provider_with_unsupported_protocol() -> None:
    """PR2: an unsupported protocol is a hard filter, not a raised UnsupportedProtocolError."""
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

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke("Hello")

    assert "anthropic" in str(exc_info.value)
    assert exc_info.value.exclusions[0].reason is FilterReason.UNSUPPORTED_PROTOCOL


def test_router_excludes_tool_request_when_provider_lacks_tool_support() -> None:
    """PR2: a missing required capability excludes the provider instead of raising."""

    class FakeAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, request: RouterRequest) -> RouterResponse:
            raise AssertionError("adapter should not be called")

    router = ProviderRouter(providers=[_openai_config()], adapter_factory=FakeAdapter)
    request = RouterRequest(
        messages=[ChatMessage(role="user", content="Use a tool")],
        requires_tools=True,
    )

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(request)

    assert "provider_a" in str(exc_info.value)
    assert "tool-calling" in str(exc_info.value)
    assert exc_info.value.exclusions[0].reason is FilterReason.MISSING_TOOLS
