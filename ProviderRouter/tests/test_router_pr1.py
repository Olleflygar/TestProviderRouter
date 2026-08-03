from __future__ import annotations

import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ConfigError,
    DuplicateCallVariantProtocolError,
    FilterReason,
    ModelArgumentConflictError,
    ProviderConfig,
    ProviderRouter,
)
from nygen_router.errors import (
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
)


def _openai_config(name: str = "provider_a", *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        provider_id=name,
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
        enabled=enabled,
    )


def _calls() -> list[CallVariant]:
    return [
        CallVariant(
            call_type=CallType.REGULAR,
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={"messages": [{"role": "user", "content": "Hello"}]},
        )
    ]


def test_router_raises_no_providers_configured_with_no_providers() -> None:
    router = ProviderRouter(metrics_scope="test", providers=[])

    with pytest.raises(NoProvidersConfiguredError):
        router.invoke(_calls())


def test_duplicate_provider_ids_are_rejected_at_construction() -> None:
    """Stable IDs key runtime state, so a duplicate would merge providers."""
    with pytest.raises(ConfigError) as exc_info:
        ProviderRouter(
            metrics_scope="test",
            providers=[
                _openai_config("provider_a"),
                _openai_config("provider_a"),
                _openai_config("provider_b"),
            ],
        )

    message = str(exc_info.value)
    assert "provider_a" in message
    assert "provider_b" not in message  # only the ID that is actually duplicated


def test_duplicate_provider_ids_error_names_every_duplicate() -> None:
    with pytest.raises(ConfigError) as exc_info:
        ProviderRouter(
            metrics_scope="test",
            providers=[
                _openai_config("provider_a"),
                _openai_config("provider_a"),
                _openai_config("provider_b"),
                _openai_config("provider_b"),
            ],
        )

    message = str(exc_info.value)
    assert "provider_a" in message
    assert "provider_b" in message


def test_router_invokes_first_enabled_openai_compatible_provider() -> None:
    """Inject a fake adapter (no real HTTP) via adapter_factory to check provider selection only."""
    called_with: dict[str, str] = {}

    class FakeAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, operation: str, arguments: dict[str, object]) -> str:
            called_with["provider_name"] = self.config.name
            return self.config.name

    router = ProviderRouter(
        metrics_scope="test",
        providers=[
            _openai_config("provider_a", enabled=False),
            _openai_config("provider_b", enabled=True),
        ],
        adapter_factory=FakeAdapter,
    )

    response = router.invoke(_calls())

    assert called_with["provider_name"] == "provider_b"
    assert response == "provider_b"


def test_router_excludes_provider_with_unsupported_protocol() -> None:
    """An unsupported protocol is a hard filter, not a raised UnsupportedProtocolError."""
    router = ProviderRouter(
        metrics_scope="test",
        providers=[
            ProviderConfig(
                provider_id="anthropic",
                name="anthropic",
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                model="claude-model",
                api_key="secret",
            )
        ],
    )

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(_calls())

    assert "anthropic" in str(exc_info.value)
    assert exc_info.value.exclusions[0].reason is FilterReason.UNSUPPORTED_PROTOCOL


def test_router_supports_custom_protocol_via_supported_protocols_param() -> None:
    """A custom adapter_factory can widen protocol support by passing the matching set."""

    class FakeAnthropicAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, operation: str, arguments: dict[str, object]) -> str:
            return self.config.name

    router = ProviderRouter(
        metrics_scope="test",
        providers=[
            ProviderConfig(
                provider_id="anthropic",
                name="anthropic",
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                model="claude-model",
                api_key="secret",
            )
        ],
        adapter_factory=FakeAnthropicAdapter,
        supported_protocols={ApiProtocol.ANTHROPIC_MESSAGES},
    )

    response = router.invoke(
        [
            CallVariant(
                call_type=CallType.REGULAR,
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                operation="messages.create",
                arguments={"messages": [{"role": "user", "content": "hi"}]},
            )
        ]
    )

    assert response == "anthropic"


def test_model_argument_conflict_raises_before_any_provider_is_contacted() -> None:
    class FakeAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, operation: str, arguments: dict[str, object]) -> str:
            raise AssertionError("adapter should not be called")

    router = ProviderRouter(
        metrics_scope="test", providers=[_openai_config()], adapter_factory=FakeAdapter
    )

    with pytest.raises(ModelArgumentConflictError):
        router.invoke(
            [
                CallVariant(
                    call_type=CallType.REGULAR,
                    protocol=ApiProtocol.OPENAI_CHAT,
                    operation="chat.completions.create",
                    arguments={"model": "sneaky", "messages": []},
                )
            ]
        )


def test_duplicate_call_variant_protocol_raises_before_any_provider_is_contacted() -> None:
    class FakeAdapter:
        def __init__(self, config: ProviderConfig):
            self.config = config

        def invoke(self, operation: str, arguments: dict[str, object]) -> str:
            raise AssertionError("adapter should not be called")

    router = ProviderRouter(
        metrics_scope="test", providers=[_openai_config()], adapter_factory=FakeAdapter
    )

    with pytest.raises(DuplicateCallVariantProtocolError):
        router.invoke(
            [
                CallVariant(
                    call_type=CallType.REGULAR,
                    protocol=ApiProtocol.OPENAI_CHAT,
                    operation="chat.completions.create",
                    arguments={"messages": []},
                ),
                CallVariant(
                    call_type=CallType.REGULAR,
                    protocol=ApiProtocol.OPENAI_CHAT,
                    operation="chat.completions.create",
                    arguments={"messages": []},
                ),
            ]
        )
