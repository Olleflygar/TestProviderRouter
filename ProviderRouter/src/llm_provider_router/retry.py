from __future__ import annotations

import warnings
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from llm_provider_router.config import ApiProtocol
from llm_provider_router.errors import ConfigError, ErrorCategory
from llm_provider_router.types import CallType

_MAX_ATTEMPTS = 8
_RETRYABLE_CATEGORIES = frozenset(
    {
        ErrorCategory.TIMEOUT,
        ErrorCategory.CONNECTION,
        ErrorCategory.SERVER_ERROR,
    }
)


class RetryProviderScope(StrEnum):
    """Which reached providers may receive one explicit retry cycle."""

    FIRST = "first"
    ALL = "all"
    SELECTED = "selected"


@dataclass(frozen=True)
class RetryContext:
    """Immutable facts about one failed physical attempt.

    The context deliberately excludes provider configuration, native arguments,
    API keys, adapters, health state, metrics stores, and fallback state.
    """

    provider_id: str
    provider_name: str
    model: str
    protocol: ApiProtocol
    error: Exception
    category: ErrorCategory
    attempt_number: int
    provider_order_index: int
    is_initial_provider: bool
    call_type: CallType
    stream_opened: bool
    newly_benched: bool


class RetryPolicy(Protocol):
    """Decide whether a failed pre-open attempt should immediately be replayed."""

    @property
    def max_attempts(self) -> int:
        """Finite ceiling of total physical attempts in one provider retry cycle."""
        ...

    def should_retry(self, context: RetryContext) -> bool:
        """Return exactly True to retry this provider, or exactly False to fall back."""
        ...


@dataclass(frozen=True, init=False)
class SameProviderRetryPolicy:
    """Retry selected providers for fixed transient failure categories.

    This object stores immutable normalized configuration only. Attempt counters
    remain local to each ``ProviderRouter.invoke`` execution, so one policy can
    be shared across routers and concurrent synchronous calls.

    Selecting a retry policy is router-wide acceptance of replay risk. A
    timeout or disconnect does not prove the provider failed to process a
    native request; retry can duplicate work, tools, side effects,
    stored/background operations, or charges. The router never inspects opaque
    arguments or creates/verifies idempotency mechanisms.
    """

    _max_attempts: int
    _provider_scope: RetryProviderScope
    _provider_ids: tuple[str, ...] | None

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        provider_scope: RetryProviderScope = RetryProviderScope.FIRST,
        provider_ids: list[str] | None = None,
    ) -> None:
        object.__setattr__(
            self,
            "_max_attempts",
            _normalize_max_attempts(max_attempts, subject="SameProviderRetryPolicy.max_attempts"),
        )
        normalized_scope = _normalize_provider_scope(provider_scope)
        object.__setattr__(self, "_provider_scope", normalized_scope)
        object.__setattr__(
            self,
            "_provider_ids",
            _normalize_provider_ids(normalized_scope, provider_ids),
        )

    @property
    def max_attempts(self) -> int:
        """Effective total-attempt ceiling, including the initial attempt."""
        return self._max_attempts

    @property
    def provider_scope(self) -> RetryProviderScope:
        """Normalized targeting scope."""
        return self._provider_scope

    @property
    def provider_ids(self) -> tuple[str, ...] | None:
        """Immutable normalized selected IDs, or None outside SELECTED mode."""
        return self._provider_ids

    def validate_provider_ids(self, configured_provider_ids: Collection[str]) -> None:
        """Reject every selected ID absent from the owning router configuration."""
        if self._provider_ids is None:
            return
        configured = set(configured_provider_ids)
        unknown = [
            provider_id for provider_id in self._provider_ids if provider_id not in configured
        ]
        if unknown:
            rendered = ", ".join(repr(provider_id) for provider_id in unknown)
            raise ConfigError(f"Unknown retry provider ID(s): {rendered}.")

    def should_retry(self, context: RetryContext) -> bool:
        """Retry a targeted, unbenched pre-open transient failure below the ceiling."""
        if context.category not in _RETRYABLE_CATEGORIES:
            return False
        if context.attempt_number >= self._max_attempts:
            return False
        if context.newly_benched or context.stream_opened:
            return False
        if self._provider_scope is RetryProviderScope.FIRST:
            return context.is_initial_provider
        if self._provider_scope is RetryProviderScope.ALL:
            return True
        return self._provider_ids is not None and context.provider_id in self._provider_ids


def _normalize_max_attempts(value: object, *, subject: str) -> int:
    """Validate and clamp one public retry ceiling at its configuration boundary."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{subject} must be an integer total-attempt count")
    if value < 2:
        raise ConfigError(f"{subject} must be at least 2 total attempts")
    if value > _MAX_ATTEMPTS:
        warnings.warn(
            f"{subject} requested {value} total attempts; using the supported maximum "
            f"of {_MAX_ATTEMPTS} total attempts.",
            UserWarning,
            stacklevel=3,
        )
        return _MAX_ATTEMPTS
    return value


def _normalize_provider_scope(value: object) -> RetryProviderScope:
    if not isinstance(value, (str, RetryProviderScope)):
        allowed = ", ".join(repr(scope.value) for scope in RetryProviderScope)
        raise ConfigError(f"provider_scope must be one of {allowed}; received {value!r}")
    try:
        return RetryProviderScope(value)
    except ValueError as exc:
        allowed = ", ".join(repr(scope.value) for scope in RetryProviderScope)
        raise ConfigError(f"provider_scope must be one of {allowed}; received {value!r}") from exc


def _normalize_provider_ids(
    scope: RetryProviderScope, provider_ids: object
) -> tuple[str, ...] | None:
    if scope is not RetryProviderScope.SELECTED:
        if provider_ids is not None:
            raise ConfigError(f"provider_ids must be None when provider_scope is {scope.value!r}")
        return None

    if not isinstance(provider_ids, list):
        raise ConfigError(
            "provider_ids must be a nonempty list of provider ID strings when "
            "provider_scope is 'selected'"
        )
    if not provider_ids:
        raise ConfigError("provider_ids must contain at least one provider ID")

    normalized: list[str] = []
    for provider_id in provider_ids:
        if not isinstance(provider_id, str):
            raise ConfigError("Selected retry provider IDs must be strings")
        value = provider_id.strip()
        if not value:
            raise ConfigError("Selected retry provider IDs must not be empty or whitespace-only")
        normalized.append(value)

    counts = Counter(normalized)
    duplicates = sorted(provider_id for provider_id, count in counts.items() if count > 1)
    if duplicates:
        rendered = ", ".join(repr(provider_id) for provider_id in duplicates)
        raise ConfigError(f"Duplicate selected retry provider ID(s): {rendered}.")
    return tuple(normalized)
