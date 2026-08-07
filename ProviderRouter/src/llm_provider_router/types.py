from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_serializer

from llm_provider_router.config import ApiProtocol


class CallType(StrEnum):
    """The response contract declared by the caller for one invocation."""

    REGULAR = "regular"
    STREAMING = "streaming"


class CallVariant(BaseModel):
    """One native, protocol-specific call, passed straight through to the provider SDK.

    ``arguments`` is never inspected or validated beyond being a mapping -- the
    provider SDK/API is the sole authority on whether its contents are valid.
    ``operation`` is a dotted SDK method path (e.g. ``"chat.completions.create"``),
    resolved dynamically against the provider's client at dispatch time.
    """

    model_config = ConfigDict(extra="forbid")

    protocol: ApiProtocol
    operation: str
    call_type: CallType
    arguments: dict[str, object]


class FilterReason(StrEnum):
    """Why a provider was excluded by a hard filter before any call was made."""

    DISABLED = "disabled"
    AUTH_DISABLED_THIS_RUN = "auth_disabled_this_run"
    IN_COOLDOWN = "in_cooldown"
    MISSING_API_KEY = "missing_api_key"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    NO_MATCHING_CALL_VARIANT = "no_matching_call_variant"


class EligibilityResult(BaseModel):
    """One excluded provider, with its specific reason and human-readable detail."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    provider_name: str
    reason: FilterReason
    detail: str


class ProviderAttempt(BaseModel):
    """One provider actually invoked during a call.

    ``error`` holds the provider's real exception object on failure (never a
    router-rephrased summary), so ``arbitrary_types_allowed`` is required.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    provider_id: str
    provider_name: str
    success: bool
    error: Exception | None = None

    @field_serializer("error", when_used="json")
    def _serialize_error(self, error: Exception | None) -> str | None:
        """JSON dumps get "TypeName: message"; attribute access keeps the real object."""
        return None if error is None else f"{type(error).__name__}: {error}"
