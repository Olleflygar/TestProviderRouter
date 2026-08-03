from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    HealthConfig,
    InvalidOperationArgumentsError,
    MetricsEvent,
    NormalizedStream,
    ProviderConfig,
    ProviderHTTPError,
    ProviderRouter,
    ProviderStreamInterruptedError,
    ProviderTimeoutError,
    RouterExhaustedError,
    RoutingContext,
    StreamFailurePolicy,
    StreamRestart,
)


class _FakeStream(NormalizedStream):
    """Stands in for a provider's SDK stream, scripted chunk by chunk.

    ``marker_at`` is the index of the chunk carrying the completion marker,
    mirroring the finish_reason chunk a real stream ends on; None means this
    provider never marks the stream complete. ``error`` is raised once the
    scripted chunks run out -- already a router error, as NormalizedStream
    requires of anything leaving ``__next__``.
    """

    def __init__(
        self,
        chunks: list[Any],
        *,
        marker_at: int | None = None,
        error: Exception | None = None,
        recognized: bool = True,
        completed: bool = False,
        usage: Any = None,
    ) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self._marker_at = marker_at
        self._error = error
        self._completed = completed
        self._recognized = recognized
        self._usage = usage
        self.close_calls = 0

    def __next__(self) -> Any:
        if self._index >= len(self._chunks):
            if self._error is not None:
                raise self._error
            raise StopIteration
        chunk = self._chunks[self._index]
        if self._index == self._marker_at:
            self._completed = True
        self._index += 1
        return chunk

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def recognized(self) -> bool:
        return self._recognized

    @property
    def usage(self) -> Any:
        return self._usage

    def close(self) -> None:
        self.close_calls += 1


def _finishing(chunks: list[Any], **kwargs: Any) -> _FakeStream:
    """A stream whose last chunk carries the completion marker."""
    return _FakeStream(chunks, marker_at=len(chunks) - 1, **kwargs)


class _Script:
    """Per-provider behavior for successive attempts, plus a record of what ran.

    Each provider gets a queue consumed one entry per attempt: a stream to
    return, any other object to return as a plain response, or an exception to
    raise at open. An unscripted attempt is a test bug, not a silent success.
    """

    def __init__(self, behaviors: dict[str, list[Any]]) -> None:
        self._behaviors = {name: list(queue) for name, queue in behaviors.items()}
        self.invoked: list[str] = []

    def next_for(self, name: str) -> Any:
        self.invoked.append(name)
        queue = self._behaviors.get(name)
        if not queue:
            raise AssertionError(f"provider {name!r} was invoked with no behavior scripted")
        behavior = queue.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _ScriptedAdapter:
    def __init__(self, config: ProviderConfig, script: _Script) -> None:
        self.config = config
        self._script = script

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        return self._script.next_for(self.config.name)


class _StaticPolicy:
    """Try eligible providers in config order (no rotation) for deterministic tests."""

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(eligible)


class _FakeStore:
    """In-memory MetricsStore fake, injected via metrics_store= (no monkeypatching)."""

    def __init__(self) -> None:
        self.events: list[MetricsEvent] = []

    def record_attempt(self, event: MetricsEvent) -> None:
        self.events.append(event)

    def query_recent(
        self,
        *,
        since: datetime,
        metrics_scope: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        protocol: ApiProtocol | None = None,
        call_type: CallType | None = None,
    ) -> list[MetricsEvent]:
        return list(self.events)


def _config(name: str) -> ProviderConfig:
    return ProviderConfig(
        provider_id=name,
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
    )


def _calls(operation: str = "chat.completions.create") -> list[CallVariant]:
    return [
        CallVariant(
            call_type=CallType.STREAMING,
            protocol=ApiProtocol.OPENAI_CHAT,
            operation=operation,
            arguments={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    ]


def _timeout(name: str) -> ProviderTimeoutError:
    return ProviderTimeoutError(
        f"Provider {name!r} timed out", provider_id=name, provider_name=name, model="model-a"
    )


def _router(
    providers: list[ProviderConfig],
    script: _Script,
    **kwargs: Any,
) -> tuple[ProviderRouter, _FakeStore]:
    store = _FakeStore()

    def factory(config: ProviderConfig) -> _ScriptedAdapter:
        return _ScriptedAdapter(config, script)

    router = ProviderRouter(
        metrics_scope="test",
        providers=providers,
        adapter_factory=factory,
        policy=_StaticPolicy(),
        metrics_store=store,  # type: ignore[arg-type]
        **kwargs,
    )
    return router, store


def test_non_streaming_call_returns_the_identical_response_object() -> None:
    """A response that is not a NormalizedStream passes through exactly as before this PR."""
    response = object()
    script = _Script({"provider_a": [response]})
    router, store = _router([_config("provider_a")], script)

    assert router.invoke(_calls()) is response
    assert len(store.events) == 1
    assert store.events[0].success is True
    assert store.events[0].call_type is CallType.STREAMING
    assert store.events[0].stream_opened is False
    assert store.events[0].total_duration_ms is not None
    assert store.events[0].total_duration_ms >= 0


def test_chunks_pass_through_unchanged_and_in_order() -> None:
    chunks: list[Any] = [object(), object(), object()]
    script = _Script({"provider_a": [_finishing(chunks)]})
    router, _ = _router([_config("provider_a")], script)

    received = list(router.invoke(_calls()))

    assert len(received) == len(chunks)
    assert all(got is expected for got, expected in zip(received, chunks, strict=True))


def test_mid_stream_failure_falls_back_and_reports_the_restart() -> None:
    error = _timeout("provider_a")
    dying = _FakeStream(["a", "b"], error=error)
    script = _Script({"provider_a": [dying], "provider_b": [_finishing(["x", "y"])]})
    restarts: list[StreamRestart] = []
    router, store = _router(
        [_config("provider_a"), _config("provider_b")], script, on_restart=restarts.append
    )

    stream = router.invoke(_calls())
    chunks = list(stream)

    assert chunks == ["a", "b", "x", "y"]
    assert stream.restarts == 1
    assert dying.close_calls == 1
    (restart,) = restarts
    assert restart.failed_provider == "provider_a"
    assert restart.failed_provider_id == "provider_a"
    assert restart.next_provider == "provider_b"
    assert restart.next_provider_id == "provider_b"
    assert restart.chunks_yielded == 2
    assert restart.restart_count == 1
    assert restart.error is error  # the provider's real, unwrapped exception
    assert [(event.provider_name, event.success) for event in store.events] == [
        ("provider_a", False),
        ("provider_b", True),
    ]


def test_failure_before_any_chunk_falls_back_without_firing_on_restart() -> None:
    """Nothing was yielded, so the consumer has nothing to discard and nothing to hear about."""
    script = _Script(
        {
            "provider_a": [_FakeStream([], error=_timeout("provider_a"))],
            "provider_b": [_finishing(["x"])],
        }
    )
    restarts: list[StreamRestart] = []
    router, _ = _router(
        [_config("provider_a"), _config("provider_b")], script, on_restart=restarts.append
    )

    assert list(router.invoke(_calls())) == ["x"]
    assert restarts == []


def test_empty_completed_stream_is_failed_and_restarted_without_a_callback() -> None:
    """A completion claim cannot turn a zero-chunk response into a served call."""
    empty = _FakeStream([], completed=True)
    script = _Script({"provider_a": [empty], "provider_b": [_finishing(["x"])]})
    restarts: list[StreamRestart] = []
    router, store = _router(
        [_config("provider_a"), _config("provider_b")],
        script,
        health=HealthConfig(failure_threshold=1),
        on_restart=restarts.append,
    )

    stream = router.invoke(_calls())

    assert list(stream) == ["x"]
    assert stream.restarts == 1
    assert restarts == []
    assert script.invoked == ["provider_a", "provider_b"]
    assert [(event.provider_name, event.success) for event in store.events] == [
        ("provider_a", False),
        ("provider_b", True),
    ]
    failed = store.events[0]
    assert failed.error_type == "stream_interrupted"
    assert failed.stream_opened is True
    assert failed.latency_ms is None
    assert failed.total_duration_ms is not None
    report = router.health_report()["provider_a"]
    assert report.consecutive_failures == 1
    assert report.cooldown_remaining_seconds is not None


def test_empty_stream_respects_raise_policy_even_when_its_shape_is_unrecognized() -> None:
    """RAISE still stops immediately; zero chunks override the shape blind spot."""
    script = _Script(
        {
            "provider_a": [_FakeStream([], recognized=False)],
            "provider_b": [_finishing(["x"])],
        }
    )
    router, store = _router(
        [_config("provider_a"), _config("provider_b")],
        script,
        stream_failure_policy=StreamFailurePolicy.RAISE,
    )

    with pytest.raises(ProviderStreamInterruptedError, match="without yielding any chunks"):
        list(router.invoke(_calls()))

    assert script.invoked == ["provider_a"]
    assert len(store.events) == 1
    assert store.events[0].success is False
    assert store.events[0].error_type == "stream_interrupted"
    assert router.health_report()["provider_a"].consecutive_failures == 1


def test_empty_streams_exhaust_every_provider_with_each_failure_visible() -> None:
    script = _Script(
        {
            "provider_a": [_FakeStream([], completed=True)],
            "provider_b": [_FakeStream([], recognized=False)],
        }
    )
    router, store = _router([_config("provider_a"), _config("provider_b")], script)

    with pytest.raises(RouterExhaustedError) as exc_info:
        list(router.invoke(_calls()))

    assert script.invoked == ["provider_a", "provider_b"]
    assert [attempt.provider_name for attempt in exc_info.value.attempts] == [
        "provider_a",
        "provider_b",
    ]
    assert all(
        isinstance(attempt.error, ProviderStreamInterruptedError)
        for attempt in exc_info.value.attempts
    )
    assert "without yielding any chunks" in str(exc_info.value)
    assert [(event.provider_name, event.success) for event in store.events] == [
        ("provider_a", False),
        ("provider_b", False),
    ]


def test_stream_ending_without_completion_marker_is_recorded_as_interrupted() -> None:
    truncated = _FakeStream(["a"], marker_at=None)
    script = _Script({"provider_a": [truncated], "provider_b": [_finishing(["x"])]})
    router, store = _router([_config("provider_a"), _config("provider_b")], script)

    assert list(router.invoke(_calls())) == ["a", "x"]
    assert store.events[0].provider_name == "provider_a"
    assert store.events[0].success is False
    assert store.events[0].error_type == "stream_interrupted"
    assert store.events[1].success is True


def test_restart_without_a_callback_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))],
            "provider_b": [_finishing(["x"])],
        }
    )
    router, _ = _router([_config("provider_a"), _config("provider_b")], script)

    with caplog.at_level(logging.WARNING, logger="nygen_router.router"):
        list(router.invoke(_calls()))

    discard_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "Discarding" in record.getMessage()
    ]
    assert len(discard_warnings) == 1
    assert "provider_b" in discard_warnings[0]


def test_on_restart_callback_exception_propagates_to_the_consumer() -> None:
    """A consumer's own restart handling failing is not the router's to swallow."""

    def explode(restart: StreamRestart) -> None:
        raise RuntimeError("callback said no")

    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))],
            "provider_b": [_finishing(["x"])],
        }
    )
    router, _ = _router([_config("provider_a"), _config("provider_b")], script, on_restart=explode)

    with pytest.raises(RuntimeError, match="callback said no"):
        list(router.invoke(_calls()))


def test_raise_policy_reraises_the_providers_real_error_without_falling_back() -> None:
    error = _timeout("provider_a")
    script = _Script({"provider_a": [_FakeStream(["a"], error=error)]})
    router, store = _router(
        [_config("provider_a"), _config("provider_b")],
        script,
        stream_failure_policy=StreamFailurePolicy.RAISE,
    )

    stream = router.invoke(_calls())
    assert next(stream) == "a"
    with pytest.raises(ProviderTimeoutError) as exc_info:
        next(stream)

    assert exc_info.value is error  # the provider's own error, not a router summary
    assert script.invoked == ["provider_a"]  # provider_b was never tried
    assert len(store.events) == 1
    assert store.events[0].success is False


@pytest.mark.parametrize(
    "policy", [StreamFailurePolicy.RESTART, StreamFailurePolicy.RAISE], ids=["restart", "raise"]
)
def test_stop_category_failure_aborts_under_both_policies(policy: StreamFailurePolicy) -> None:
    """A broken call is not a provider problem: no fallback, and no provider is blamed."""
    error = InvalidOperationArgumentsError(
        "arguments rejected", provider_id="provider_a", provider_name="provider_a", model="model-a"
    )
    script = _Script({"provider_a": [_FakeStream(["a"], error=error)]})
    router, store = _router(
        [_config("provider_a"), _config("provider_b")], script, stream_failure_policy=policy
    )

    stream = router.invoke(_calls())
    assert next(stream) == "a"
    with pytest.raises(InvalidOperationArgumentsError) as exc_info:
        next(stream)

    assert exc_info.value is error
    assert script.invoked == ["provider_a"]
    assert store.events[0].error_type == "invalid_operation"
    assert router.health_report()["provider_a"].consecutive_failures == 0


def test_every_provider_failing_mid_stream_raises_with_each_distinct_reason() -> None:
    first = _timeout("provider_a")
    second = ProviderHTTPError(
        provider_id="provider_b",
        provider_name="provider_b",
        model="model-a",
        status_code=503,
        message="upstream down",
    )
    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=first)],
            "provider_b": [_FakeStream([], error=second)],
        }
    )
    router, _ = _router([_config("provider_a"), _config("provider_b")], script)

    with pytest.raises(RouterExhaustedError) as exc_info:
        list(router.invoke(_calls()))

    message = str(exc_info.value)
    assert "timed out" in message
    assert "503" in message
    attempts = exc_info.value.attempts
    assert [attempt.provider_name for attempt in attempts] == ["provider_a", "provider_b"]
    assert attempts[0].error is first
    assert attempts[1].error is second


def test_pre_stream_failures_are_carried_into_the_exhausted_error() -> None:
    """An attempt that never opened a stream still appears, with its own real reason."""
    opening_failure = _timeout("provider_a")
    mid_stream_failure = _timeout("provider_b")
    script = _Script(
        {
            "provider_a": [opening_failure],
            "provider_b": [_FakeStream(["x"], error=mid_stream_failure)],
        }
    )
    router, store = _router([_config("provider_a"), _config("provider_b")], script)

    with pytest.raises(RouterExhaustedError) as exc_info:
        list(router.invoke(_calls()))

    assert [attempt.provider_name for attempt in exc_info.value.attempts] == [
        "provider_a",
        "provider_b",
    ]
    # The attempt that never opened a stream is not recorded as a stream row.
    assert [event.stream_opened for event in store.events] == [False, True]


def test_breaking_and_closing_the_loop_closes_the_underlying_stream() -> None:
    underlying = _finishing(["a", "b", "c"])
    script = _Script({"provider_a": [underlying]})
    router, _ = _router([_config("provider_a")], script)

    stream = router.invoke(_calls())
    for _chunk in stream:
        break
    stream.close()

    assert underlying.close_calls == 1


def test_context_manager_closes_the_underlying_stream() -> None:
    underlying = _finishing(["a", "b"])
    script = _Script({"provider_a": [underlying]})
    router, _ = _router([_config("provider_a")], script)

    with router.invoke(_calls()) as stream:
        assert next(stream) == "a"

    assert underlying.close_calls == 1


def test_close_before_the_completion_marker_records_nothing() -> None:
    """An outcome the caller declined to observe is not one the router can report."""
    script = _Script({"provider_a": [_finishing(["a", "b"])]})
    router, store = _router([_config("provider_a")], script)

    stream = router.invoke(_calls())
    assert next(stream) == "a"
    stream.close()

    assert store.events == []
    assert router.health_report()["provider_a"].consecutive_failures == 0


def test_close_after_the_completion_marker_records_the_observed_success() -> None:
    """The break-on-finish_reason pattern still feeds scoring history."""
    script = _Script({"provider_a": [_FakeStream(["a", "b"], marker_at=0)]})
    router, store = _router([_config("provider_a")], script)

    stream = router.invoke(_calls())
    assert next(stream) == "a"
    stream.close()

    assert len(store.events) == 1
    assert store.events[0].success is True
    assert store.events[0].stream_opened is True


def test_close_is_idempotent_and_terminal() -> None:
    underlying = _FakeStream(["a", "b"], marker_at=0)
    script = _Script({"provider_a": [underlying]})
    router, store = _router([_config("provider_a")], script)

    stream = router.invoke(_calls())
    assert next(stream) == "a"
    stream.close()
    stream.close()

    assert underlying.close_calls == 1
    assert len(store.events) == 1  # not recorded twice either
    with pytest.raises(StopIteration):
        next(stream)
    assert list(stream) == []


def test_bare_break_without_close_records_nothing() -> None:
    """No router code runs, so there is no outcome to record."""
    script = _Script({"provider_a": [_finishing(["a", "b"])]})
    router, store = _router([_config("provider_a")], script)

    for _chunk in router.invoke(_calls()):
        break

    assert store.events == []


def test_stream_metrics_record_ttft_and_total_duration_at_stream_end() -> None:
    script = _Script({"provider_a": [_finishing(["a", "b"])]})
    router, store = _router([_config("provider_a")], script)

    stream = router.invoke(_calls())
    assert store.events == []  # nothing recorded merely because the stream opened

    list(stream)

    (event,) = store.events
    assert event.success is True
    assert event.stream_opened is True
    assert event.latency_ms is not None
    assert event.total_duration_ms is not None
    assert event.total_duration_ms >= event.latency_ms


def test_stream_that_never_yielded_a_chunk_records_no_ttft() -> None:
    """latency_ms means time-to-first-chunk, and there was no first chunk."""
    script = _Script({"provider_a": [_FakeStream([], error=_timeout("provider_a"))]})
    router, store = _router([_config("provider_a")], script)

    with pytest.raises(RouterExhaustedError):
        list(router.invoke(_calls()))

    (event,) = store.events
    assert event.stream_opened is True
    assert event.latency_ms is None
    assert event.total_duration_ms is not None  # open to death is still measured


def test_mid_stream_failure_records_total_duration_and_logs_it() -> None:
    script = _Script({"provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))]})
    router, store = _router([_config("provider_a")], script)

    with pytest.raises(RouterExhaustedError):
        list(router.invoke(_calls()))

    (event,) = store.events
    assert event.success is False
    assert event.stream_opened is True
    assert event.latency_ms is not None
    assert event.total_duration_ms is not None


def test_unrecognized_stream_shape_counts_a_clean_end_as_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The router never invents a failure it cannot evidence."""
    script = _Script({"provider_a": [_FakeStream(["a"], marker_at=None, recognized=False)]})
    router, store = _router([_config("provider_a"), _config("provider_b")], script)

    with caplog.at_level(logging.DEBUG, logger="nygen_router.router"):
        assert list(router.invoke(_calls())) == ["a"]

    assert script.invoked == ["provider_a"]  # no fallback: nothing failed
    (event,) = store.events
    assert event.success is True
    assert event.stream_opened is True
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "chat.completions.create" in warnings[0].getMessage()


def test_unrecognized_stream_shape_warns_once_per_operation_then_drops_to_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = _Script(
        {
            "provider_a": [
                _FakeStream(["a"], marker_at=None, recognized=False),
                _FakeStream(["b"], marker_at=None, recognized=False),
            ]
        }
    )
    router, _ = _router([_config("provider_a")], script)

    with caplog.at_level(logging.DEBUG, logger="nygen_router.router"):
        list(router.invoke(_calls()))
        list(router.invoke(_calls()))

    shape_records = [
        record for record in caplog.records if "silent-truncation" in record.getMessage()
    ]
    assert [record.levelno for record in shape_records] == [logging.WARNING, logging.DEBUG]


def test_unrecognized_stream_shape_still_falls_back_on_a_mid_stream_exception() -> None:
    """Exception-based fallback is untouched by the shape the chunks came in."""
    script = _Script(
        {
            "provider_a": [
                _FakeStream(["a"], recognized=False, error=_timeout("provider_a")),
            ],
            "provider_b": [_finishing(["x"])],
        }
    )
    router, _ = _router([_config("provider_a"), _config("provider_b")], script)

    assert list(router.invoke(_calls())) == ["a", "x"]


def test_health_records_at_stream_end_so_a_dying_stream_reaches_the_threshold() -> None:
    """Success at stream open would let a provider oscillate and never bench."""
    script = _Script(
        {
            "provider_a": [
                _timeout("provider_a"),
                _FakeStream(["a"], error=_timeout("provider_a")),
            ],
            "provider_b": [_finishing(["x"]), _finishing(["y"])],
        }
    )
    router, _ = _router(
        [_config("provider_a"), _config("provider_b")],
        script,
        health=HealthConfig(failure_threshold=2),
    )

    list(router.invoke(_calls()))  # provider_a fails at open, provider_b serves
    assert router.health_report()["provider_a"].consecutive_failures == 1

    stream = router.invoke(_calls())
    assert router.health_report()["provider_a"].consecutive_failures == 1  # still, at open
    list(stream)

    report = router.health_report()["provider_a"]
    assert report.consecutive_failures == 2
    assert report.cooldown_remaining_seconds is not None  # benched at the threshold


def test_stream_interrupted_counts_toward_the_failure_threshold() -> None:
    """A chronically truncating provider is a broken provider, and never a STOP category."""
    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], marker_at=None)],
            "provider_b": [_finishing(["x"])],
        }
    )
    router, _ = _router([_config("provider_a"), _config("provider_b")], script)

    list(router.invoke(_calls()))

    assert script.invoked == ["provider_a", "provider_b"]  # fell back, so not a STOP category
    assert router.health_report()["provider_a"].consecutive_failures == 1


def test_restart_returning_a_non_stream_is_rejected_and_fallback_continues() -> None:
    """Chunks of two generations must never interleave, and a response is not a stream."""

    class _Closable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    anomaly = _Closable()
    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))],
            "provider_b": [anomaly],
            "provider_c": [_finishing(["x"])],
        }
    )
    router, store = _router(
        [_config("provider_a"), _config("provider_b"), _config("provider_c")], script
    )

    assert list(router.invoke(_calls())) == ["a", "x"]
    assert anomaly.closed is True
    assert [(event.provider_name, event.success) for event in store.events] == [
        ("provider_a", False),
        ("provider_b", False),
        ("provider_c", True),
    ]


def test_a_restart_that_cannot_open_keeps_falling_back() -> None:
    """A provider that fails at open during a restart is one more dead attempt, not the end."""
    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))],
            "provider_b": [_timeout("provider_b")],
            "provider_c": [_finishing(["x"])],
        }
    )
    router, store = _router(
        [_config("provider_a"), _config("provider_b"), _config("provider_c")], script
    )

    assert list(router.invoke(_calls())) == ["a", "x"]
    assert script.invoked == ["provider_a", "provider_b", "provider_c"]
    assert [(event.provider_name, event.stream_opened) for event in store.events] == [
        ("provider_a", True),
        ("provider_b", False),  # no stream ever opened for provider_b
        ("provider_c", True),
    ]


def test_a_non_stream_restart_response_with_nothing_to_close_is_still_rejected() -> None:
    """ "Close it if possible" -- a response with no close() is discarded just the same."""
    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))],
            "provider_b": [object()],
            "provider_c": [_finishing(["x"])],
        }
    )
    router, _ = _router(
        [_config("provider_a"), _config("provider_b"), _config("provider_c")], script
    )

    assert list(router.invoke(_calls())) == ["a", "x"]


def test_a_non_stream_restart_response_that_cannot_be_closed_is_still_rejected() -> None:
    """Cleanup failing is not a reason to hand a whole response to a chunk consumer."""

    class _UnclosableResponse:
        def close(self) -> None:
            raise RuntimeError("close failed")

    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))],
            "provider_b": [_UnclosableResponse()],
            "provider_c": [_finishing(["x"])],
        }
    )
    router, _ = _router(
        [_config("provider_a"), _config("provider_b"), _config("provider_c")], script
    )

    assert list(router.invoke(_calls())) == ["a", "x"]


def test_a_stream_whose_close_fails_still_reports_its_outcome() -> None:
    """A failure to release the connection must not mask what the provider actually did."""

    class _UnclosableStream(_FakeStream):
        def close(self) -> None:
            raise RuntimeError("close failed")

    script = _Script({"provider_a": [_UnclosableStream(["a"], marker_at=0)]})
    router, store = _router([_config("provider_a")], script)

    assert list(router.invoke(_calls())) == ["a"]
    assert len(store.events) == 1
    assert store.events[0].success is True


def test_a_normalized_stream_is_recognized_by_default() -> None:
    """A custom adapter implements completed; recognized defaults to trusting it."""

    class _MinimalStream(NormalizedStream):
        def __next__(self) -> Any:
            raise StopIteration

        @property
        def completed(self) -> bool:
            return True

        @property
        def usage(self) -> Any:
            return None

        def close(self) -> None:
            return None

    assert _MinimalStream().recognized is True


def test_each_provider_is_tried_at_most_once_per_call() -> None:
    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))],
            "provider_b": [_FakeStream(["b"], error=_timeout("provider_b"))],
        }
    )
    router, _ = _router([_config("provider_a"), _config("provider_b")], script)

    with pytest.raises(RouterExhaustedError):
        list(router.invoke(_calls()))

    assert script.invoked == ["provider_a", "provider_b"]


def test_a_stop_category_at_restart_stops_the_search() -> None:
    """A broken call discovered while restarting must not be tried against everyone else."""
    script = _Script(
        {
            "provider_a": [_FakeStream(["a"], error=_timeout("provider_a"))],
            "provider_b": [
                InvalidOperationArgumentsError(
                    "arguments rejected",
                    provider_id="provider_b",
                    provider_name="provider_b",
                    model="model-a",
                )
            ],
        }
    )
    router, _ = _router(
        [_config("provider_a"), _config("provider_b"), _config("provider_c")], script
    )

    with pytest.raises(RouterExhaustedError) as exc_info:
        list(router.invoke(_calls()))

    assert script.invoked == ["provider_a", "provider_b"]  # provider_c never tried
    assert [attempt.provider_name for attempt in exc_info.value.attempts] == [
        "provider_a",
        "provider_b",
    ]
