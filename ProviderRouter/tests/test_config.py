from __future__ import annotations

import pytest
from pydantic import ValidationError

from nygen_router import ApiProtocol, ProviderConfig
from nygen_router.errors import MissingApiKeyError


def test_valid_openai_compatible_config() -> None:
    config = ProviderConfig(
        name="provider_a",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://api.example.com/v1",
        api_key="secret",
    )

    assert config.name == "provider_a"
    assert config.protocol == ApiProtocol.OPENAI_CHAT


def test_empty_name_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(
            name=" ",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="model-a",
            base_url="https://api.example.com/v1",
            api_key="secret",
        )


def test_empty_model_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model=" ",
            base_url="https://api.example.com/v1",
            api_key="secret",
        )


def test_missing_base_url_rejected_for_openai_chat() -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="model-a",
            api_key="secret",
        )


def test_missing_base_url_rejected_for_openai_responses() -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_RESPONSES,
            model="model-a",
            api_key="secret",
        )


def test_missing_api_key_and_api_key_env_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="model-a",
            base_url="https://api.example.com/v1",
        )


def test_explicit_api_key_resolves() -> None:
    config = ProviderConfig(
        name="provider_a",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://api.example.com/v1",
        api_key="secret",
    )

    assert config.resolve_api_key() == "secret"


def test_api_key_env_resolves_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER_A_API_KEY", "secret-from-env")
    config = ProviderConfig(
        name="provider_a",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://api.example.com/v1",
        api_key_env="PROVIDER_A_API_KEY",
    )

    assert config.resolve_api_key() == "secret-from-env"


def test_missing_env_var_raises_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROVIDER_A_API_KEY", raising=False)
    config = ProviderConfig(
        name="provider_a",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://api.example.com/v1",
        api_key_env="PROVIDER_A_API_KEY",
    )

    with pytest.raises(MissingApiKeyError) as exc_info:
        config.resolve_api_key()

    assert exc_info.value.provider_name == "provider_a"
    assert exc_info.value.env_var == "PROVIDER_A_API_KEY"
