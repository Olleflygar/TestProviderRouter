from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    ConfigError,
    ErrorCategory,
    ProviderConfig,
    ProviderRouter,
    RetryContext,
    RetryPolicy,
    RetryProviderScope,
    SameProviderRetryPolicy,
)


def _provider(provider_id: str, *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=f"display-{provider_id}",
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url="https://example.com/v1",
        api_key="secret",
        enabled=enabled,
    )


def _context(
    *,
    category: ErrorCategory = ErrorCategory.TIMEOUT,
    provider_id: str = "a",
    attempt_number: int = 1,
    provider_order_index: int = 0,
    stream_opened: bool = False,
    newly_benched: bool = False,
) -> RetryContext:
    return RetryContext(
        provider_id=provider_id,
        provider_name=f"display-{provider_id}",
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        error=RuntimeError("boom"),
        category=category,
        attempt_number=attempt_number,
        provider_order_index=provider_order_index,
        is_initial_provider=provider_order_index == 0,
        call_type=CallType.REGULAR,
        stream_opened=stream_opened,
        newly_benched=newly_benched,
    )


def test_retry_public_api_is_exported_and_structurally_typed() -> None:
    policy: RetryPolicy = SameProviderRetryPolicy()

    assert policy.max_attempts == 3
    assert ErrorCategory.TIMEOUT.value == "timeout"
    assert RetryProviderScope.FIRST.value == "first"


@pytest.mark.parametrize("value", [True, False, 2.0, "3", None])
def test_max_attempts_rejects_bool_and_non_integer_values(value: object) -> None:
    with pytest.raises(ConfigError, match="integer total-attempt count"):
        SameProviderRetryPolicy(max_attempts=cast(Any, value))


@pytest.mark.parametrize("value", [-1, 0, 1])
def test_max_attempts_rejects_values_below_two(value: int) -> None:
    with pytest.raises(ConfigError, match="at least 2 total attempts"):
        SameProviderRetryPolicy(max_attempts=value)


def test_max_attempts_accepts_eight_and_clamps_larger_value_once_at_caller() -> None:
    assert SameProviderRetryPolicy(max_attempts=8).max_attempts == 8

    with pytest.warns(UserWarning) as warnings:
        policy = SameProviderRetryPolicy(max_attempts=12)

    assert policy.max_attempts == 8
    assert len(warnings) == 1
    assert "12" in str(warnings[0].message)
    assert "8" in str(warnings[0].message)
    assert Path(warnings[0].filename) == Path(__file__)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (RetryProviderScope.FIRST, RetryProviderScope.FIRST),
        ("first", RetryProviderScope.FIRST),
        ("all", RetryProviderScope.ALL),
        ("selected", RetryProviderScope.SELECTED),
    ],
)
def test_provider_scope_accepts_enum_and_matching_strings(
    value: object, expected: RetryProviderScope
) -> None:
    provider_ids = ["a"] if expected is RetryProviderScope.SELECTED else None
    policy = SameProviderRetryPolicy(provider_scope=cast(Any, value), provider_ids=provider_ids)

    assert policy.provider_scope is expected


@pytest.mark.parametrize("value", ["FIRST", "unknown", 1, None])
def test_invalid_provider_scope_raises_config_error(value: object) -> None:
    with pytest.raises(ConfigError, match="provider_scope must be one of"):
        SameProviderRetryPolicy(provider_scope=cast(Any, value))


@pytest.mark.parametrize("scope", [RetryProviderScope.FIRST, RetryProviderScope.ALL])
def test_first_and_all_reject_provider_ids(scope: RetryProviderScope) -> None:
    with pytest.raises(ConfigError, match="provider_ids must be None"):
        SameProviderRetryPolicy(provider_scope=scope, provider_ids=["a"])


@pytest.mark.parametrize("value", [None, (), ("a",), {"a"}, "a"])
def test_selected_requires_an_actual_nonempty_list(value: object) -> None:
    with pytest.raises(ConfigError, match="nonempty list"):
        SameProviderRetryPolicy(
            provider_scope=RetryProviderScope.SELECTED,
            provider_ids=cast(Any, value),
        )


def test_selected_rejects_empty_list_non_strings_blanks_and_trimmed_duplicates() -> None:
    with pytest.raises(ConfigError, match="at least one"):
        SameProviderRetryPolicy(provider_scope="selected", provider_ids=[])
    with pytest.raises(ConfigError, match="must be strings"):
        SameProviderRetryPolicy(provider_scope="selected", provider_ids=cast(Any, [1]))
    with pytest.raises(ConfigError, match="must not be empty"):
        SameProviderRetryPolicy(provider_scope="selected", provider_ids=["  "])
    with pytest.raises(ConfigError, match="Duplicate.*'a'"):
        SameProviderRetryPolicy(provider_scope="selected", provider_ids=[" a", "a "])


def test_selected_ids_are_trimmed_defensively_copied_and_read_only() -> None:
    configured = ["  a  "]
    policy = SameProviderRetryPolicy(provider_scope="selected", provider_ids=configured)
    configured[0] = "changed"

    assert policy.provider_ids == ("a",)
    with pytest.raises(AttributeError):
        policy.max_attempts = 7  # type: ignore[misc]


def test_router_reports_all_unknown_selected_ids_without_accepting_display_names() -> None:
    policy = SameProviderRetryPolicy(
        provider_scope="selected",
        provider_ids=["display-known", "missing"],
    )

    with pytest.raises(ConfigError) as exc_info:
        ProviderRouter(
            providers=[_provider("known")],
            metrics_scope="test",
            metrics_store=None,
            retry_policy=policy,
        )

    assert "display-known" in str(exc_info.value)
    assert "missing" in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_known_disabled_selected_id_is_valid_configuration() -> None:
    ProviderRouter(
        providers=[_provider("known", enabled=False)],
        metrics_scope="test",
        metrics_store=None,
        retry_policy=SameProviderRetryPolicy(provider_scope="selected", provider_ids=["known"]),
    )


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.TIMEOUT,
        ErrorCategory.CONNECTION,
        ErrorCategory.SERVER_ERROR,
    ],
)
def test_builtin_retries_exact_fixed_transient_categories(category: ErrorCategory) -> None:
    assert SameProviderRetryPolicy().should_retry(_context(category=category)) is True


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.UNKNOWN,
        ErrorCategory.STREAM_INTERRUPTED,
        ErrorCategory.BAD_REQUEST,
        ErrorCategory.INVALID_OPERATION,
        ErrorCategory.AUTH,
        ErrorCategory.RATE_LIMIT,
    ],
)
def test_builtin_rejects_every_non_transient_category(category: ErrorCategory) -> None:
    assert SameProviderRetryPolicy().should_retry(_context(category=category)) is False


def test_builtin_scope_ceiling_stream_and_bench_decisions() -> None:
    first = SameProviderRetryPolicy(max_attempts=2)
    all_providers = SameProviderRetryPolicy(provider_scope="all")
    selected = SameProviderRetryPolicy(provider_scope="selected", provider_ids=["b"])

    assert first.should_retry(_context(attempt_number=2)) is False
    assert first.should_retry(_context(provider_order_index=1)) is False
    assert first.should_retry(_context(stream_opened=True)) is False
    assert first.should_retry(_context(newly_benched=True)) is False
    assert all_providers.should_retry(_context(provider_order_index=2)) is True
    assert selected.should_retry(_context(provider_id="a")) is False
    assert selected.should_retry(_context(provider_id="b", provider_order_index=1)) is True


def test_retry_context_is_frozen() -> None:
    context = _context()

    with pytest.raises(FrozenInstanceError):
        context.attempt_number = 2  # type: ignore[misc]
