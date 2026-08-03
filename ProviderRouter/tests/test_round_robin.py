from __future__ import annotations

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ProviderConfig,
    ProviderRouter,
    RoundRobinPolicy,
    RoutingContext,
)


def _config(name: str, *, enabled: bool = True) -> ProviderConfig:
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
            arguments={"messages": [{"role": "user", "content": "hi"}]},
        )
    ]


class _EchoAdapter:
    """Always succeeds, echoing back which provider served the call."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    def invoke(self, operation: str, arguments: dict[str, object]) -> str:
        return self.config.name


class _ReversePolicy:
    """Attempt eligible providers in reverse order (a fake policy for injection)."""

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(reversed(eligible))


def test_round_robin_rotates_starting_provider() -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    router = ProviderRouter(metrics_scope="test", providers=providers, adapter_factory=_EchoAdapter)

    selected = [router.invoke(_calls()) for _ in range(4)]

    assert selected == ["provider_a", "provider_b", "provider_c", "provider_a"]


def test_round_robin_only_rotates_among_eligible_providers() -> None:
    providers = [
        _config("provider_a"),
        _config("provider_b", enabled=False),  # filtered out, never selected
        _config("provider_c"),
    ]
    router = ProviderRouter(metrics_scope="test", providers=providers, adapter_factory=_EchoAdapter)

    selected = [router.invoke(_calls()) for _ in range(4)]

    assert "provider_b" not in selected
    assert selected == ["provider_a", "provider_c", "provider_a", "provider_c"]


def test_injected_policy_is_honored() -> None:
    """A fake Policy passed via the constructor seam overrides the default rotation."""
    providers = [_config("provider_a"), _config("provider_b")]
    router = ProviderRouter(
        metrics_scope="test",
        providers=providers,
        adapter_factory=_EchoAdapter,
        policy=_ReversePolicy(),
    )

    # Reverse order puts provider_b first, and it succeeds immediately every call.
    assert router.invoke(_calls()) == "provider_b"
    assert router.invoke(_calls()) == "provider_b"


def test_round_robin_order_of_empty_eligible_is_empty() -> None:
    context = RoutingContext(metrics_scope="test", call_type=CallType.REGULAR, metrics_store=None)

    assert RoundRobinPolicy().order([], context) == []


def test_round_robin_accepts_context_without_changing_rotation() -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    context = RoutingContext(metrics_scope="test", call_type=CallType.REGULAR, metrics_store=None)
    policy = RoundRobinPolicy()

    assert policy.order(providers, context) == providers
    assert policy.order(providers, context) == [providers[1], providers[2], providers[0]]
