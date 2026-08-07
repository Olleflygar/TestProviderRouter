from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from metrics_store_helpers import aggregate_events_for_score_query

from llm_provider_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ConfigError,
    ErrorCategory,
    HealthConfig,
    MetricsEvent,
    ProviderConfig,
    ProviderConnectionError,
    ProviderHTTPError,
    ProviderRouter,
    ProviderTimeoutError,
    RetryContext,
    RetryProviderScope,
    RouterExhaustedError,
    RoutingContext,
    SameProviderRetryPolicy,
    ScoreAggregate,
    ScoreAggregateQuery,
    ScoreBasedPolicy,
    StickyRoutingPolicy,
    UnsupportedOperationError,
)


def _provider(
    provider_id: str,
    *,
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT,
    model: str | None = None,
    enabled: bool = True,
) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=f"display-{provider_id}",
        protocol=protocol,
        model=model or f"model-{provider_id}",
        base_url=f"https://{provider_id}.example.com/v1",
        api_key="secret",
        enabled=enabled,
    )


def _calls(call_type: CallType = CallType.REGULAR) -> list[CallVariant]:
    return [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=call_type,
            arguments={"messages": [{"role": "user", "content": "hi"}]},
        )
    ]


def _mixed_calls() -> list[CallVariant]:
    return [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.REGULAR,
            arguments={"messages": []},
        ),
        CallVariant(
            protocol=ApiProtocol.OPENAI_RESPONSES,
            operation="responses.create",
            call_type=CallType.REGULAR,
            arguments={"input": "hi"},
        ),
    ]


def _timeout(provider_id: str, text: str | None = None) -> ProviderTimeoutError:
    return ProviderTimeoutError(
        text or f"{provider_id} timed out",
        provider_id=provider_id,
        provider_name=f"display-{provider_id}",
        model=f"model-{provider_id}",
    )


def _connection(provider_id: str) -> ProviderConnectionError:
    return ProviderConnectionError(
        f"{provider_id} disconnected",
        provider_id=provider_id,
        provider_name=f"display-{provider_id}",
        model=f"model-{provider_id}",
    )


def _http(provider_id: str, status: int) -> ProviderHTTPError:
    return ProviderHTTPError(
        provider_id=provider_id,
        provider_name=f"display-{provider_id}",
        model=f"model-{provider_id}",
        status_code=status,
        message=f"status {status}",
    )


class _Script:
    def __init__(self, behaviors: dict[str, list[Any]]) -> None:
        self.behaviors = {provider_id: list(items) for provider_id, items in behaviors.items()}
        self.invoked: list[str] = []
        self.operations: list[tuple[str, str, str]] = []
        self.argument_objects: list[dict[str, object]] = []
        self.adapter_ids: list[int] = []
        self.factories: list[str] = []

    def run(
        self,
        provider: ProviderConfig,
        adapter_id: int,
        operation: str,
        arguments: dict[str, object],
    ) -> Any:
        self.invoked.append(provider.provider_id)
        self.operations.append((provider.provider_id, operation, str(arguments["model"])))
        self.argument_objects.append(arguments)
        self.adapter_ids.append(adapter_id)
        arguments["adapter_mutation"] = len(self.invoked)
        queue = self.behaviors.get(provider.provider_id)
        if not queue:
            raise AssertionError(f"No scripted behavior for {provider.provider_id!r}")
        behavior = queue.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _Adapter:
    def __init__(self, provider: ProviderConfig, script: _Script) -> None:
        self.provider = provider
        self.script = script

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        return self.script.run(self.provider, id(self), operation, arguments)


class _StaticPolicy:
    def __init__(self, result: list[ProviderConfig] | None = None) -> None:
        self.result = result
        self.calls = 0

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        self.calls += 1
        return list(eligible if self.result is None else self.result)


class _MemoryStore:
    def __init__(self, events: list[MetricsEvent] | None = None) -> None:
        self.events = [] if events is None else list(events)

    def record_attempt(self, event: MetricsEvent) -> None:
        self.events.append(event)

    def query_recent(self, **kwargs: Any) -> list[MetricsEvent]:
        return list(self.events)

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        return aggregate_events_for_score_query(self.events, query)


class _FailingStore(_MemoryStore):
    def record_attempt(self, event: MetricsEvent) -> None:
        raise RuntimeError("storage unavailable")


class _RecordingRetryPolicy:
    def __init__(self, *, max_attempts: int = 3, decisions: list[Any] | None = None) -> None:
        self.max_attempts = max_attempts
        self.decisions = [True] if decisions is None else list(decisions)
        self.contexts: list[RetryContext] = []

    def should_retry(self, context: RetryContext) -> bool:
        self.contexts.append(context)
        if not self.decisions:
            return True
        return self.decisions.pop(0)  # type: ignore[no-any-return]


def _router(
    providers: list[ProviderConfig],
    script: _Script,
    *,
    retry_policy: Any = None,
    policy: Any = None,
    metrics_store: Any = None,
    health: HealthConfig | None = None,
) -> ProviderRouter:
    def factory(provider: ProviderConfig) -> _Adapter:
        script.factories.append(provider.provider_id)
        return _Adapter(provider, script)

    return ProviderRouter(
        providers=providers,
        metrics_scope="test",
        metrics_store=metrics_store,
        adapter_factory=factory,
        policy=_StaticPolicy() if policy is None else policy,
        retry_policy=retry_policy,
        health=health,
    )


@pytest.mark.parametrize("explicit_none", [False, True])
def test_omission_and_explicit_none_preserve_one_attempt_per_ordered_provider(
    explicit_none: bool,
) -> None:
    a, b = _provider("a"), _provider("b")
    response = object()
    script = _Script({"a": [_timeout("a")], "b": [response]})
    kwargs = {"retry_policy": None} if explicit_none else {}

    router = _router([a, b], script, **kwargs)

    assert router.invoke(_calls()) is response
    assert script.invoked == ["a", "b"]


def test_first_attempt_success_never_consults_retry_policy_and_preserves_identity() -> None:
    response = object()
    policy = _RecordingRetryPolicy()
    script = _Script({"a": [response]})

    assert _router([_provider("a")], script, retry_policy=policy).invoke(_calls()) is response
    assert policy.contexts == []


def test_first_transient_recovery_uses_three_total_attempts_with_fresh_arguments() -> None:
    response = object()
    store = _MemoryStore()
    original_arguments = _calls()[0].arguments
    calls = _calls()
    script = _Script({"a": [_timeout("a", "one"), _connection("a"), response]})
    router = _router(
        [_provider("a")],
        script,
        retry_policy=SameProviderRetryPolicy(),
        metrics_store=store,
    )

    assert router.invoke(calls) is response
    assert script.invoked == ["a", "a", "a"]
    assert script.factories == ["a"]
    assert len(set(script.adapter_ids)) == 1
    assert len({id(arguments) for arguments in script.argument_objects}) == 3
    assert all(arguments["model"] == "model-a" for arguments in script.argument_objects)
    assert calls[0].arguments == original_arguments
    assert [event.success for event in store.events] == [False, False, True]
    assert router.health_report()["a"].consecutive_failures == 0


def test_first_cycle_exhausts_then_each_fallback_gets_one_base_attempt() -> None:
    a, b, c = _provider("a"), _provider("b"), _provider("c")
    response = object()
    script = _Script(
        {
            "a": [_timeout("a"), _timeout("a"), _timeout("a")],
            "b": [_timeout("b")],
            "c": [response],
        }
    )

    result = _router([a, b, c], script, retry_policy=SameProviderRetryPolicy()).invoke(_calls())

    assert result is response
    assert script.invoked == ["a", "a", "a", "b", "c"]


@pytest.mark.parametrize(
    ("retry_policy", "expected"),
    [
        (
            SameProviderRetryPolicy(max_attempts=2, provider_scope=RetryProviderScope.ALL),
            ["a", "a", "b", "b", "c"],
        ),
        (
            SameProviderRetryPolicy(
                max_attempts=2,
                provider_scope=RetryProviderScope.SELECTED,
                provider_ids=["b"],
            ),
            ["a", "b", "b", "c"],
        ),
    ],
)
def test_all_and_selected_target_only_their_reached_providers(
    retry_policy: SameProviderRetryPolicy, expected: list[str]
) -> None:
    providers = [_provider(item) for item in "abc"]
    script = _Script(
        {
            "a": [_timeout("a"), _timeout("a")],
            "b": [_timeout("b"), _timeout("b")],
            "c": [object()],
        }
    )

    _router(providers, script, retry_policy=retry_policy).invoke(_calls())

    assert script.invoked == expected


def test_selected_filtered_and_custom_policy_omitted_providers_are_not_resurrected() -> None:
    disabled = _provider("disabled", enabled=False)
    a = _provider("a")
    omitted = _provider("omitted")
    script = _Script({"a": [object()]})
    ordering = _StaticPolicy(result=[a])
    retry = SameProviderRetryPolicy(provider_scope="selected", provider_ids=["disabled", "omitted"])

    _router([disabled, a, omitted], script, retry_policy=retry, policy=ordering).invoke(_calls())

    assert script.invoked == ["a"]
    assert ordering.calls == 1


def test_score_based_first_retries_only_the_highest_scoring_provider() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    high, low = _provider("high"), _provider("low")
    history = [
        MetricsEvent(
            metrics_scope="test",
            provider_id=provider_id,
            provider_name=f"display-{provider_id}",
            model=f"model-{provider_id}",
            protocol=ApiProtocol.OPENAI_CHAT,
            call_type=CallType.REGULAR,
            success=success,
            latency_ms=latency,
            timestamp=now,
        )
        for provider_id, success, latency in [
            *(("high", True, 10.0) for _ in range(20)),
            *(("low", False, None) for _ in range(20)),
        ]
    ]
    store = _MemoryStore(history)
    script = _Script(
        {
            "high": [_timeout("high"), _timeout("high"), _timeout("high")],
            "low": [object()],
        }
    )

    _router(
        [low, high],
        script,
        retry_policy=SameProviderRetryPolicy(),
        policy=ScoreBasedPolicy(now=lambda: now),
        metrics_store=store,
    ).invoke(_calls())

    assert script.invoked == ["high", "high", "high", "low"]


def test_sticky_first_retries_only_first_eligible_sticky_and_never_rewrites_preference() -> None:
    a, b, tail = _provider("a"), _provider("b"), _provider("tail")
    script = _Script(
        {
            "a": [_timeout("a"), _timeout("a"), _timeout("a"), "later-a"],
            "b": [_timeout("b")],
            "tail": ["fallback"],
        }
    )
    router = _router(
        [tail, b, a],
        script,
        retry_policy=SameProviderRetryPolicy(),
        policy=StickyRoutingPolicy(sticky_provider_ids=["a", "b"], fallback_policy=_StaticPolicy()),
    )

    assert router.invoke(_calls()) == "fallback"
    router.reset_health("a")
    assert router.invoke(_calls()) == "later-a"
    assert script.invoked == ["a", "a", "a", "b", "tail", "a"]


@pytest.mark.parametrize("status", [401, 429])
def test_auth_and_rate_limit_never_retry_and_fall_back(status: int) -> None:
    script = _Script({"a": [_http("a", status)], "b": [object()]})

    _router(
        [_provider("a"), _provider("b")],
        script,
        retry_policy=_RecordingRetryPolicy(max_attempts=8),
    ).invoke(_calls())

    assert script.invoked == ["a", "b"]


@pytest.mark.parametrize(
    "error",
    [
        _http("a", 400),
        UnsupportedOperationError(
            "bad operation",
            provider_id="a",
            provider_name="display-a",
            model="model-a",
        ),
    ],
)
def test_stop_categories_never_retry_or_fall_back(error: Exception) -> None:
    policy = _RecordingRetryPolicy(max_attempts=8)
    script = _Script({"a": [error], "b": [object()]})

    with pytest.raises(RouterExhaustedError) as exc_info:
        _router([_provider("a"), _provider("b")], script, retry_policy=policy).invoke(_calls())

    assert script.invoked == ["a"]
    assert exc_info.value.attempts[0].error is error
    assert policy.contexts == []


def test_later_hard_gate_failure_stops_a_retry_cycle_globally() -> None:
    bad = _http("a", 400)
    script = _Script({"a": [_timeout("a"), bad], "b": [object()]})

    with pytest.raises(RouterExhaustedError) as exc_info:
        _router(
            [_provider("a"), _provider("b")],
            script,
            retry_policy=SameProviderRetryPolicy(max_attempts=8),
        ).invoke(_calls())

    assert script.invoked == ["a", "a"]
    assert exc_info.value.attempts[1].error is bad


def test_new_bench_stops_retry_cycle_immediately_and_fallback_continues() -> None:
    script = _Script({"a": [_timeout("a"), _timeout("a")], "b": [object()]})
    router = _router(
        [_provider("a"), _provider("b")],
        script,
        retry_policy=SameProviderRetryPolicy(max_attempts=8),
        health=HealthConfig(failure_threshold=2),
    )

    router.invoke(_calls())

    assert script.invoked == ["a", "a", "b"]
    assert router.health_report()["a"].consecutive_failures == 2


def test_preexisting_failure_count_can_bench_initial_attempt_before_retry() -> None:
    script = _Script(
        {"a": [_timeout("a"), _timeout("a")], "b": ["first-fallback", "second-fallback"]}
    )
    retry = _RecordingRetryPolicy(max_attempts=8, decisions=[False, True])
    router = _router(
        [_provider("a"), _provider("b")],
        script,
        retry_policy=retry,
        health=HealthConfig(failure_threshold=2),
    )

    assert router.invoke(_calls()) == "first-fallback"
    assert router.invoke(_calls()) == "second-fallback"
    assert script.invoked == ["a", "b", "a", "b"]
    assert len(retry.contexts) == 1


def test_every_physical_failure_is_preserved_in_exhaustion_order_with_identity() -> None:
    errors = [_timeout("a", "one"), _timeout("a", "two"), _timeout("a", "three")]
    tail_error = _connection("b")
    script = _Script({"a": errors.copy(), "b": [tail_error]})

    with pytest.raises(RouterExhaustedError) as exc_info:
        _router(
            [_provider("a"), _provider("b")],
            script,
            retry_policy=SameProviderRetryPolicy(),
        ).invoke(_calls())

    attempts = exc_info.value.attempts
    assert [attempt.provider_id for attempt in attempts] == ["a", "a", "a", "b"]
    assert [attempt.error for attempt in attempts] == [*errors, tail_error]


def test_cross_protocol_retry_stays_native_then_fallback_uses_own_variant_and_model() -> None:
    chat = _provider("chat", protocol=ApiProtocol.OPENAI_CHAT, model="chat-model")
    responses = _provider(
        "responses", protocol=ApiProtocol.OPENAI_RESPONSES, model="responses-model"
    )
    result = object()
    script = _Script(
        {
            "chat": [_timeout("chat"), _timeout("chat"), _timeout("chat")],
            "responses": [result],
        }
    )

    assert (
        _router([chat, responses], script, retry_policy=SameProviderRetryPolicy()).invoke(
            _mixed_calls()
        )
        is result
    )
    assert script.operations == [
        ("chat", "chat.completions.create", "chat-model"),
        ("chat", "chat.completions.create", "chat-model"),
        ("chat", "chat.completions.create", "chat-model"),
        ("responses", "responses.create", "responses-model"),
    ]


def test_storage_failure_does_not_invalidate_later_retry_success() -> None:
    response = object()
    script = _Script({"a": [_timeout("a"), response]})

    assert (
        _router(
            [_provider("a")],
            script,
            retry_policy=SameProviderRetryPolicy(),
            metrics_store=_FailingStore(),
        ).invoke(_calls())
        is response
    )


def test_custom_policy_receives_exact_frozen_context_and_may_retry_unknown() -> None:
    unknown = RuntimeError("custom transient")
    response = object()
    policy = _RecordingRetryPolicy(decisions=[True])
    script = _Script({"a": [unknown, response]})

    assert _router([_provider("a")], script, retry_policy=policy).invoke(_calls()) is response

    context = policy.contexts[0]
    assert context.error is unknown
    assert context.category is ErrorCategory.UNKNOWN
    assert context.attempt_number == 1
    assert context.provider_order_index == 0
    assert context.is_initial_provider is True
    assert context.stream_opened is False
    assert context.newly_benched is False


@pytest.mark.parametrize("decision", [1, 0, "yes", None])
def test_custom_policy_decision_must_be_exact_bool(decision: object) -> None:
    policy = _RecordingRetryPolicy(decisions=[decision])
    script = _Script({"a": [_timeout("a")]})

    with pytest.raises(ConfigError, match="return exactly bool"):
        _router([_provider("a")], script, retry_policy=policy).invoke(_calls())

    assert script.invoked == ["a"]


def test_custom_policy_exception_propagates_after_attempt_recording() -> None:
    marker = RuntimeError("policy failed")
    store = _MemoryStore()

    class _RaisingRetry:
        max_attempts = 3

        def should_retry(self, context: RetryContext) -> bool:
            raise marker

    with pytest.raises(RuntimeError) as exc_info:
        _router(
            [_provider("a")],
            _Script({"a": [_timeout("a")]}),
            retry_policy=_RaisingRetry(),
            metrics_store=store,
        ).invoke(_calls())

    assert exc_info.value is marker
    assert [event.success for event in store.events] == [False]


def test_custom_ceiling_is_cached_clamped_warned_and_never_mutates_policy() -> None:
    policy = _RecordingRetryPolicy(max_attempts=12)
    response = object()
    script = _Script({"a": [*[_timeout("a") for _ in range(8)], response], "b": [response]})

    with pytest.warns(UserWarning) as warnings:
        router = _router(
            [_provider("a"), _provider("b")],
            script,
            retry_policy=policy,
            health=HealthConfig(failure_threshold=100),
        )
    policy.max_attempts = 100
    assert router.invoke(_calls()) is response

    assert len(warnings) == 1
    assert Path(warnings[0].filename) == Path(__file__)
    assert policy.max_attempts == 100
    assert script.invoked == [*(["a"] * 8), "b"]


@pytest.mark.parametrize("value", [True, 1, 1.5])
def test_custom_policy_max_attempts_uses_same_minimum_and_type_validation(value: object) -> None:
    policy = _RecordingRetryPolicy(max_attempts=3)
    policy.max_attempts = value  # type: ignore[assignment]

    with pytest.raises(ConfigError):
        _router([_provider("a")], _Script({"a": [object()]}), retry_policy=policy)


def test_custom_order_duplicates_remain_base_attempts_but_only_one_retry_cycle() -> None:
    a, b = _provider("a"), _provider("b")
    response = object()
    ordering = _StaticPolicy(result=[a, a, b])
    script = _Script(
        {
            "a": [_timeout("a"), _timeout("a"), _timeout("a"), _timeout("a")],
            "b": [response],
        }
    )

    assert (
        _router(
            [a, b],
            script,
            retry_policy=SameProviderRetryPolicy(),
            policy=ordering,
        ).invoke(_calls())
        is response
    )
    assert script.invoked == ["a", "a", "a", "a", "b"]
    assert script.factories == ["a", "a", "b"]
    assert ordering.calls == 1


def test_shared_builtin_policy_keeps_concurrent_invocation_counters_isolated() -> None:
    retry = SameProviderRetryPolicy(max_attempts=2)

    def invoke_once(index: int) -> tuple[int, list[str]]:
        response = object()
        script = _Script({"a": [_timeout("a"), response]})
        result = _router([_provider("a")], script, retry_policy=retry).invoke(_calls())
        assert result is response
        return index, script.invoked

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(invoke_once, range(32)))

    assert [index for index, _ in results] == list(range(32))
    assert all(invoked == ["a", "a"] for _, invoked in results)
