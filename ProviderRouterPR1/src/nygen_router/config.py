from __future__ import annotations

import os
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from nygen_router.errors import MissingApiKeyError


class ApiProtocol(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supports_chat: bool = True
    supports_responses_api: bool = False
    supports_tools: bool = False
    supports_streaming: bool = False
    supports_json_mode: bool = False


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    protocol: ApiProtocol
    model: str
    base_url: str | None = None
    api_key: SecretStr | None = None
    api_key_env: str | None = None
    enabled: bool = True
    timeout_seconds: float = 30.0
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)

    @field_validator("name", "model")
    @classmethod
    def _must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("base_url", "api_key_env")
    @classmethod
    def _blank_strings_become_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_api_key_becomes_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> Self:
        if self.protocol == ApiProtocol.OPENAI_CHAT and self.base_url is None:
            raise ValueError("base_url is required for OPENAI_CHAT providers.")
        if self.api_key is None and self.api_key_env is None:
            raise ValueError("At least one of api_key or api_key_env is required.")
        return self

    def resolve_api_key(self) -> str:
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        if self.api_key_env is not None:
            api_key = os.environ.get(self.api_key_env)
            if api_key:
                return api_key
        raise MissingApiKeyError(self.name, self.api_key_env)
