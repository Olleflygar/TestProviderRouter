from __future__ import annotations

import pytest

from nygen_router import (
    ApiProtocol,
    ChatMessage,
    FilterReason,
    ProviderCapabilities,
    ProviderConfig,
    ProviderRouter,
    RouterRequest,
    RouterResponse,
)
from nygen_router.errors import NoEligibleProvidersError
from nygen_router.filters import filter_eligible_providers

SUPPORTED = frozenset({ApiProtocol.OPENAI_CHAT})


def _config(
    name: str = "provider_a",
    *,
    enabled: bool = True,
    api_key: str | None = "secret",
    api_key_env: str | None = None,
    capabilities: ProviderCapabilities | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key=api_key,
        api_key_env=api_key_env,
        enabled=enabled,
        capabilities=capabilities or ProviderCapabilities(),
    )


def _text_request() -> RouterRequest:
    return RouterRequest(messages=[ChatMessage(role="user", content="hi")])


class _FakeAdapter:
    def __init__(self, config: ProviderConfig):
        self.config = config

    def invoke(self, request: RouterRequest) -> RouterResponse:
        return RouterResponse(provider_name=self.config.name, model=self.config.model, text="ok")


def test_disabled_provider_is_excluded() -> None:
    eligible, excluded = filter_eligible_providers(
        [_config(enabled=False)], _text_request(), supported_protocols=SUPPORTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.DISABLED


def test_provider_without_api_key_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NYGEN_TEST_MISSING_KEY", raising=False)
    provider = _config(api_key=None, api_key_env="NYGEN_TEST_MISSING_KEY")

    eligible, excluded = filter_eligible_providers(
        [provider], _text_request(), supported_protocols=SUPPORTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.MISSING_API_KEY


def test_unsupported_protocol_is_excluded() -> None:
    provider = ProviderConfig(
        name="anthropic",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        model="claude-model",
        api_key="secret",
    )

    eligible, excluded = filter_eligible_providers(
        [provider], _text_request(), supported_protocols=SUPPORTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.UNSUPPORTED_PROTOCOL


def test_provider_without_tools_excluded_when_tools_required() -> None:
    request = RouterRequest(messages=[ChatMessage(role="user", content="hi")], requires_tools=True)

    eligible, excluded = filter_eligible_providers(
        [_config()], request, supported_protocols=SUPPORTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.MISSING_TOOLS
    assert "tool-calling" in excluded[0].detail


def test_provider_without_streaming_excluded_when_streaming_required() -> None:
    request = RouterRequest(
        messages=[ChatMessage(role="user", content="hi")], requires_streaming=True
    )

    eligible, excluded = filter_eligible_providers(
        [_config()], request, supported_protocols=SUPPORTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.MISSING_STREAMING


def test_provider_without_json_mode_excluded_when_json_required() -> None:
    request = RouterRequest(
        messages=[ChatMessage(role="user", content="hi")], requires_json_mode=True
    )

    eligible, excluded = filter_eligible_providers(
        [_config()], request, supported_protocols=SUPPORTED
    )

    assert eligible == []
    assert excluded[0].reason is FilterReason.MISSING_JSON_MODE


def test_capable_provider_is_eligible() -> None:
    provider = _config(capabilities=ProviderCapabilities(supports_tools=True))
    request = RouterRequest(messages=[ChatMessage(role="user", content="hi")], requires_tools=True)

    eligible, excluded = filter_eligible_providers(
        [provider], request, supported_protocols=SUPPORTED
    )

    assert [provider.name for provider in eligible] == ["provider_a"]
    assert excluded == []


def test_all_providers_filtered_out_raises_with_each_specific_reason() -> None:
    providers = [
        _config("provider_a", enabled=False),
        _config("provider_c"),  # enabled, but lacks tool support
    ]
    router = ProviderRouter(providers=providers)
    request = RouterRequest(messages=[ChatMessage(role="user", content="hi")], requires_tools=True)

    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(request)

    message = str(exc_info.value)
    assert "provider_a: provider is disabled" in message
    assert "provider_c: missing tool-calling support" in message
    assert {result.provider_name for result in exc_info.value.exclusions} == {
        "provider_a",
        "provider_c",
    }


def test_successful_call_still_reports_filtered_providers_in_excluded() -> None:
    providers = [_config("provider_a", enabled=False), _config("provider_b")]
    router = ProviderRouter(providers=providers, adapter_factory=_FakeAdapter)

    response = router.invoke("hi")

    assert response.provider_name == "provider_b"
    assert [result.provider_name for result in response.excluded] == ["provider_a"]
    assert response.excluded[0].reason is FilterReason.DISABLED


def test_successful_call_populates_attempts_with_invoked_provider() -> None:
    router = ProviderRouter(providers=[_config("provider_b")], adapter_factory=_FakeAdapter)

    response = router.invoke("hi")

    assert len(response.attempts) == 1
    assert response.attempts[0].provider_name == "provider_b"
    assert response.attempts[0].success is True
    assert response.attempts[0].error is None
