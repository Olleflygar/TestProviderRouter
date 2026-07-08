from __future__ import annotations

from nygen_router import (
    ApiProtocol,
    ProviderConfig,
    ProviderRouter,
    RoundRobinPolicy,
    RouterResponse,
)


def _config(name: str, *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
        enabled=enabled,
    )


class _EchoAdapter:
    """Always succeeds, reporting which provider served the call."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    def invoke(self, request: object) -> RouterResponse:
        return RouterResponse(provider_name=self.config.name, model=self.config.model, text="ok")


class _ReversePolicy:
    """Attempt eligible providers in reverse order (a fake policy for injection)."""

    def order(self, eligible: list[ProviderConfig]) -> list[ProviderConfig]:
        return list(reversed(eligible))


def test_round_robin_rotates_starting_provider() -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    router = ProviderRouter(providers=providers, adapter_factory=_EchoAdapter)

    selected = [router.invoke("hi").provider_name for _ in range(4)]

    assert selected == ["provider_a", "provider_b", "provider_c", "provider_a"]


def test_round_robin_only_rotates_among_eligible_providers() -> None:
    providers = [
        _config("provider_a"),
        _config("provider_b", enabled=False),  # filtered out, never selected
        _config("provider_c"),
    ]
    router = ProviderRouter(providers=providers, adapter_factory=_EchoAdapter)

    selected = [router.invoke("hi").provider_name for _ in range(4)]

    assert "provider_b" not in selected
    assert selected == ["provider_a", "provider_c", "provider_a", "provider_c"]


def test_injected_policy_is_honored() -> None:
    """A fake Policy passed via the constructor seam overrides the default rotation."""
    providers = [_config("provider_a"), _config("provider_b")]
    router = ProviderRouter(
        providers=providers,
        adapter_factory=_EchoAdapter,
        policy=_ReversePolicy(),
    )

    # Reverse order puts provider_b first, and it succeeds immediately every call.
    assert router.invoke("hi").provider_name == "provider_b"
    assert router.invoke("hi").provider_name == "provider_b"


def test_round_robin_order_of_empty_eligible_is_empty() -> None:
    assert RoundRobinPolicy().order([]) == []
