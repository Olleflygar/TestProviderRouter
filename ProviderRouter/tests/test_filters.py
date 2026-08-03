from __future__ import annotations

import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    FilterReason,
    NoEligibleProvidersError,
    ProviderConfig,
    ProviderRouter,
)
from nygen_router.filters import filter_eligible_providers
from nygen_router.health import CooldownTrigger, ProviderHealthState

SUPPORTED = frozenset({ApiProtocol.OPENAI_CHAT})
REQUESTED = frozenset({ApiProtocol.OPENAI_CHAT})


def _config(
    name: str = "provider_a",
    *,
    enabled: bool = True,
    api_key: str | None = "secret",
    api_key_env: str | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        provider_id=name,
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key=api_key,
        api_key_env=api_key_env,
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


class _TrackingAdapter:
    """Records which provider was actually invoked and echoes its name back."""

    def __init__(self, config: ProviderConfig, invoked: list[str] | None = None):
        self.config = config
        self._invoked = invoked

    def invoke(self, operation: str, arguments: dict[str, object]) -> str:
        if self._invoked is not None:
            self._invoked.append(self.config.name)
        return self.config.name


def test_disabled_provider_is_excluded() -> None:
    eligible, excluded = filter_eligible_providers(
        [_config(enabled=False)], supported_protocols=SUPPORTED, requested_protocols=REQUESTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.DISABLED


def test_provider_without_api_key_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NYGEN_TEST_MISSING_KEY", raising=False)
    provider = _config(api_key=None, api_key_env="NYGEN_TEST_MISSING_KEY")

    eligible, excluded = filter_eligible_providers(
        [provider], supported_protocols=SUPPORTED, requested_protocols=REQUESTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.MISSING_API_KEY


def test_unsupported_protocol_is_excluded() -> None:
    provider = ProviderConfig(
        provider_id="anthropic",
        name="anthropic",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        model="claude-model",
        api_key="secret",
    )

    eligible, excluded = filter_eligible_providers(
        [provider], supported_protocols=SUPPORTED, requested_protocols=REQUESTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.UNSUPPORTED_PROTOCOL


def test_provider_without_matching_call_variant_is_excluded() -> None:
    """The router supports the protocol in general, but this call has no CallVariant for it."""
    provider = _config()

    eligible, excluded = filter_eligible_providers(
        [provider], supported_protocols=SUPPORTED, requested_protocols=frozenset()
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.NO_MATCHING_CALL_VARIANT


def test_fully_eligible_provider_passes() -> None:
    provider = _config()

    eligible, excluded = filter_eligible_providers(
        [provider], supported_protocols=SUPPORTED, requested_protocols=REQUESTED
    )

    assert [p.name for p in eligible] == ["provider_a"]
    assert excluded == []


def test_auth_benched_provider_without_a_stored_error_keeps_the_plain_detail() -> None:
    """The verbatim error enriches the detail when present; its absence must not break it."""
    health = {"provider_a": ProviderHealthState(auth_disabled=True)}

    eligible, excluded = filter_eligible_providers(
        [_config()], supported_protocols=SUPPORTED, requested_protocols=REQUESTED, health=health
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.AUTH_DISABLED_THIS_RUN
    assert excluded[0].detail == "disabled after an auth failure earlier this run"


def test_cooldown_without_a_stored_error_keeps_the_plain_detail() -> None:
    health = {
        "provider_a": ProviderHealthState(
            cooldown_until=50.0,
            consecutive_failures=3,
            cooldown_trigger=CooldownTrigger.CONSECUTIVE_FAILURES,
        )
    }

    eligible, excluded = filter_eligible_providers(
        [_config()],
        supported_protocols=SUPPORTED,
        requested_protocols=REQUESTED,
        health=health,
        now=20.0,
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.IN_COOLDOWN
    assert excluded[0].detail == "in cooldown (30.0s remaining) after 3 consecutive failures"


def test_filter_reads_health_without_mutating_it() -> None:
    """An elapsed cooldown reads as eligible, but clearing it is the router's business."""
    state = ProviderHealthState(
        cooldown_until=50.0,
        consecutive_failures=3,
        last_error="upstream read timeout",
        cooldown_trigger=CooldownTrigger.CONSECUTIVE_FAILURES,
    )

    eligible, excluded = filter_eligible_providers(
        [_config()],
        supported_protocols=SUPPORTED,
        requested_protocols=REQUESTED,
        health={"provider_a": state},
        now=100.0,  # the cooldown has long lapsed
    )

    assert [p.name for p in eligible] == ["provider_a"]
    assert excluded == []
    assert state == ProviderHealthState(
        cooldown_until=50.0,
        consecutive_failures=3,
        last_error="upstream read timeout",
        cooldown_trigger=CooldownTrigger.CONSECUTIVE_FAILURES,
    )


def test_all_providers_filtered_out_raises_with_each_specific_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NYGEN_TEST_MISSING_KEY_2", raising=False)
    providers = [
        _config("provider_a", enabled=False),
        _config("provider_c", api_key=None, api_key_env="NYGEN_TEST_MISSING_KEY_2"),
    ]
    router = ProviderRouter(metrics_scope="test", providers=providers)

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(_calls())

    message = str(exc_info.value)
    assert 'provider_a (id="provider_a"): provider is disabled' in message
    assert 'provider_c (id="provider_c"): no API key available' in message
    assert {result.provider_name for result in exc_info.value.exclusions} == {
        "provider_a",
        "provider_c",
    }


def test_successful_call_only_invokes_eligible_provider() -> None:
    """A disabled provider is filtered out and never invoked; the eligible one serves the call."""
    invoked: list[str] = []
    providers = [_config("provider_a", enabled=False), _config("provider_b")]
    router = ProviderRouter(
        metrics_scope="test",
        providers=providers,
        adapter_factory=lambda config: _TrackingAdapter(config, invoked),
    )

    response = router.invoke(_calls())

    assert response == "provider_b"
    assert invoked == ["provider_b"]  # provider_a was excluded, never invoked
