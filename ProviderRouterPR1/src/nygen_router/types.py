from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


class RouterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    requires_tools: bool = False
    requires_streaming: bool = False
    requires_json_mode: bool = False

    @classmethod
    def from_input(cls, value: str | RouterRequest) -> RouterRequest:
        """Wrap a plain string as a single user message, or pass a request through."""
        if isinstance(value, str):
            return cls(messages=[ChatMessage(role="user", content=value)])
        return value


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class FilterReason(StrEnum):
    """Why a provider was excluded by a hard filter before any call was made."""

    DISABLED = "disabled"
    AUTH_DISABLED_THIS_RUN = "auth_disabled_this_run"
    MISSING_API_KEY = "missing_api_key"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    MISSING_TOOLS = "missing_tools"
    MISSING_STREAMING = "missing_streaming"
    MISSING_JSON_MODE = "missing_json_mode"


class EligibilityResult(BaseModel):
    """One excluded provider, with its specific reason and human-readable detail."""

    model_config = ConfigDict(extra="forbid")

    provider_name: str
    reason: FilterReason
    detail: str


class ProviderAttempt(BaseModel):
    """One provider actually invoked during a call.

    ``error`` holds the provider's real exception object on failure (never a
    router-rephrased summary), so ``arbitrary_types_allowed`` is required. In
    PR2 there is no fallback, so a returned response carries exactly one
    attempt and it always succeeded.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    provider_name: str
    success: bool
    error: Exception | None = None


class RouterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str
    model: str
    text: str
    raw: dict[str, object] | None = None
    usage: TokenUsage | None = None
    attempts: list[ProviderAttempt] = Field(default_factory=list)
    excluded: list[EligibilityResult] = Field(default_factory=list)
