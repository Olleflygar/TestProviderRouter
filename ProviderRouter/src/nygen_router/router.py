from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from nygen_router.adapters.base import NormalizedStream, ProviderAdapter
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.adapters.openai_responses import OpenAIResponsesAdapter
from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.errors import (
    ConfigError,
    DuplicateCallVariantProtocolError,
    ErrorCategory,
    MixedCallTypeError,
    ModelArgumentConflictError,
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
    ProviderError,
    ProviderStreamInterruptedError,
    RouterClosedError,
    RouterExhaustedError,
    UnsupportedProtocolError,
    categorize_error,
)
from nygen_router.filters import filter_eligible_providers
from nygen_router.health import (
    HealthConfig,
    ProviderHealthReport,
    ProviderHealthState,
)
from nygen_router.metrics import MetricsEvent
from nygen_router.policies import (
    Policy,
    RoundRobinPolicy,
    RoutingContext,
    StickyRoutingPolicy,
)
from nygen_router.retry import RetryContext, RetryPolicy, SameProviderRetryPolicy
from nygen_router.retry import _normalize_max_attempts as _normalize_retry_max_attempts
from nygen_router.storage.base import MetricsStore
from nygen_router.storage.duckdb import DuckDBMetricsStore
from nygen_router.types import CallType, CallVariant, ProviderAttempt

AdapterFactory = Callable[[ProviderConfig], ProviderAdapter]

logger = logging.getLogger(__name__)

# Protocols the built-in adapter factory can serve.
SUPPORTED_PROTOCOLS = frozenset({ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES})

# Failure categories that abort the whole run immediately instead of falling
# back to the next eligible provider: the call itself is broken (malformed
# request, bad operation, mismatched arguments, missing SDK), so no other
# provider trying the same broken call would fare any better.
_STOP_CATEGORIES = frozenset({ErrorCategory.BAD_REQUEST, ErrorCategory.INVALID_OPERATION})
_NO_RETRY_CATEGORIES = frozenset(
    {
        ErrorCategory.BAD_REQUEST,
        ErrorCategory.INVALID_OPERATION,
        ErrorCategory.AUTH,
        ErrorCategory.RATE_LIMIT,
    }
)


@dataclass(frozen=True)
class _FailureOutcome:
    """Classification and health transition produced by one physical failure."""

    category: ErrorCategory
    newly_benched: bool


class StreamFailurePolicy(StrEnum):
    """What a stream that dies mid-generation should do.

    An enum rather than a bool so a third mode can arrive without a breaking
    change. RESTART is the default because dying mid-generation with no
    fallback is the worst failure mode for a long-running workflow, and that
    default must cost no configuration.
    """

    RESTART = "restart"
    RAISE = "raise"


@dataclass(frozen=True)
class StreamRestart:
    """One mid-stream switch of provider, reported to ``on_restart``.

    An object rather than positional arguments so new fields can be added
    without breaking callbacks already in the wild. ``error`` is the failed
    provider's own exception, never a router summary of it.

    ``chunks_yielded`` is what the consumer has accumulated from the provider
    that just died: the next provider regenerates from scratch, so that partial
    output must be discarded rather than concatenated.
    """

    failed_provider: str
    failed_provider_id: str
    error: Exception
    next_provider: str
    next_provider_id: str
    chunks_yielded: int
    restart_count: int


class _UnsetType:
    """Sentinel distinguishing "metrics_store not passed" from "metrics_store=None".

    None must keep meaning "disable persistence entirely", so it cannot also
    mean "use the default" -- a plain default value of None would conflate
    the two.
    """


_UNSET = _UnsetType()


class ProviderRouter:
    def __init__(
        self,
        providers: list[ProviderConfig],
        adapter_factory: AdapterFactory | None = None,
        policy: Policy | None = None,
        supported_protocols: Collection[ApiProtocol] | None = None,
        metrics_store: MetricsStore | None | _UnsetType = _UNSET,
        health: HealthConfig | Mapping[str, object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        stream_failure_policy: StreamFailurePolicy = StreamFailurePolicy.RESTART,
        on_restart: Callable[[StreamRestart], None] | None = None,
        *,
        metrics_scope: str,
        retry_policy: RetryPolicy | None = None,
    ):
        self.providers = list(providers)
        self._reject_duplicate_provider_ids(self.providers)
        self.metrics_scope = self._validate_metrics_scope(metrics_scope)
        self._adapter_factory = adapter_factory or self._default_adapter_for
        self._using_default_adapters = adapter_factory is None
        # Built-in adapters cache their SDK client, so reusing the adapter is
        # what keeps pooled HTTP connections alive across calls. Only the
        # default factory's adapters are cached: a custom adapter_factory
        # keeps its existing call-per-attempt behavior and owns its own reuse.
        self._default_adapter_cache: dict[str, ProviderAdapter] = {}
        self._policy = policy or RoundRobinPolicy()
        if isinstance(self._policy, StickyRoutingPolicy):
            self._policy.validate_provider_ids(
                [provider.provider_id for provider in self.providers]
            )
        self._retry_policy = retry_policy
        self._retry_max_attempts = (
            None
            if retry_policy is None
            else _normalize_retry_max_attempts(
                retry_policy.max_attempts,
                subject="retry_policy.max_attempts",
            )
        )
        if isinstance(retry_policy, SameProviderRetryPolicy):
            retry_policy.validate_provider_ids(
                [provider.provider_id for provider in self.providers]
            )
        # Validated at the boundary so a typo'd key raises here rather than
        # flowing on as a raw dict that silently means nothing.
        self._health_config = (
            HealthConfig() if health is None else HealthConfig.model_validate(health)
        )
        # Monotonic by default: cooldowns must not be disturbed by wall-clock
        # jumps. Injected so tests can advance time instead of sleeping.
        self._clock = clock
        # A custom adapter_factory that serves more protocols must pass the
        # matching set here, or the eligibility filter keeps excluding them.
        self._supported_protocols = (
            frozenset(supported_protocols)
            if supported_protocols is not None
            else SUPPORTED_PROTOCOLS
        )
        # Per-run provider health, visible to the eligibility filter and any
        # policy. In memory only: it lives and dies with this router instance,
        # so a new router per request accumulates no health signal at all.
        self._health: dict[str, ProviderHealthState] = {}
        # metrics_store=None disables persistence entirely; not passing it at
        # all defaults to a DuckDBMetricsStore pointed at its own default path.
        # Only that router-created default is owned by the router: close()
        # closes what the router created, never a caller-provided store.
        selected_metrics_store: object | None
        if isinstance(metrics_store, _UnsetType):
            self._owned_metrics_store: DuckDBMetricsStore | None = DuckDBMetricsStore()
            selected_metrics_store = self._owned_metrics_store
        else:
            self._owned_metrics_store = None
            selected_metrics_store = metrics_store
        self._metrics_store = self._validate_metrics_store(selected_metrics_store)
        # A missing DuckDB dependency was already reported by the store at
        # construction, so the first failed write must not repeat the warning.
        self._metrics_warning_emitted = (
            isinstance(self._metrics_store, DuckDBMetricsStore)
            and not self._metrics_store.available
        )
        self._metrics_recovery_emitted = False
        self._dropped_metrics_events = 0
        # Stream fallback is on by default; RAISE is for callers who would
        # rather stop than have a second provider regenerate the answer.
        self._stream_failure_policy = stream_failure_policy
        self._on_restart = on_restart
        # Operations whose stream shape this router has already reported as
        # unreadable, so one unfamiliar provider cannot flood the log.
        self._stream_shape_warned: set[str] = set()
        # One lock guards every piece of mutable router state: health, the
        # default adapter cache, the metrics/stream-shape counters, the closed
        # flag, and policy ordering (so built-in and custom policies alike are
        # serialized per router without locks of their own). It is never held
        # across an adapter invocation or stream iteration -- provider network
        # calls stay fully concurrent. When the store's lock is also needed,
        # this lock is always taken first; nothing ever takes them in the
        # other order, so deadlock is impossible.
        self._lock = threading.Lock()
        # close() is terminal: a closed router raises on invoke() and drops
        # (counts) any late metrics write from a still-draining stream rather
        # than reopening the connection it just closed.
        self._closed = False

    def invoke(self, calls: list[CallVariant]) -> Any:
        """Filter, order eligible providers, then try them in turn with fallback.

        Returns the raw native response object the winning provider's SDK
        returned -- untouched, with nothing attached. Every attempt and
        exclusion is still tracked internally, so a total failure still
        raises an error enumerating each provider's own real reason.

        A response that is a NormalizedStream is the one exception: its outcome
        is not known yet, so it comes back wrapped in a RouterStream that
        finishes the job -- fallback, metrics and health -- while the consumer
        iterates. Consumers write the same ``for chunk in ...`` loop either way.
        """
        with self._lock:
            if self._closed:
                raise RouterClosedError()

        if not self.providers:
            raise NoProvidersConfiguredError("No providers configured.")

        variants_by_protocol, call_type = self._prepare_variants(calls)

        # One critical section covers the health-reading eligibility filter
        # and policy ordering, so concurrent invokes see consistent health and
        # policies never run concurrently. Provider attempts below run outside
        # any lock.
        with self._lock:
            eligible, excluded = filter_eligible_providers(
                self.providers,
                supported_protocols=self._supported_protocols,
                requested_protocols=variants_by_protocol.keys(),
                health=self._health,
                now=self._clock(),
            )
            if not eligible or call_type is None:
                raise NoEligibleProvidersError(excluded)

            context = RoutingContext(
                metrics_store=self._metrics_store,
                metrics_scope=self.metrics_scope,
                call_type=call_type,
            )
            ordered = list(self._policy.order(eligible, context))

        attempts: list[ProviderAttempt] = []
        first_order_indexes: dict[str, int] = {}
        retry_cycles_seen: set[str] = set()
        for index, provider in enumerate(ordered):
            first_order_indexes.setdefault(provider.provider_id, index)
            retry_cycle_available = (
                self._retry_policy is not None and provider.provider_id not in retry_cycles_seen
            )
            if self._retry_policy is not None:
                retry_cycles_seen.add(provider.provider_id)
            variant = variants_by_protocol[provider.protocol]
            adapter = self._adapter_for(provider)
            attempt_number = 1
            while True:
                # A custom adapter may mutate its input, so every physical
                # attempt receives a fresh top-level copy from the original
                # opaque CallVariant.
                arguments = self._arguments_for(variant, provider)
                start = time.perf_counter()
                try:
                    response = adapter.invoke(variant.operation, arguments)
                except Exception as exc:
                    latency_ms = (time.perf_counter() - start) * 1000.0
                    attempts.append(
                        ProviderAttempt(
                            provider_id=provider.provider_id,
                            provider_name=provider.name,
                            success=False,
                            error=exc,
                        )
                    )
                    outcome = self._record_attempt_failure(
                        provider,
                        exc,
                        call_type=call_type,
                        latency_ms=(latency_ms if call_type is CallType.REGULAR else None),
                        stream_opened=(None if call_type is CallType.REGULAR else False),
                        total_duration_ms=(None if call_type is CallType.REGULAR else latency_ms),
                    )
                    if outcome.category in _STOP_CATEGORIES:
                        break
                    if not self._should_retry_provider(
                        provider=provider,
                        error=exc,
                        outcome=outcome,
                        attempt_number=attempt_number,
                        provider_order_index=first_order_indexes[provider.provider_id],
                        call_type=call_type,
                        retry_cycle_available=retry_cycle_available,
                    ):
                        break
                    attempt_number += 1
                    continue

                if isinstance(response, NormalizedStream):
                    # Nothing is known yet -- headers arriving is not a served call
                    # -- so no attempt, no metrics and no health change is recorded
                    # here. RouterStream carries the rest of the ranked order and
                    # records the real outcome when the stream reaches it. PR27
                    # counters never enter RouterStream after this boundary.
                    return RouterStream(
                        router=self,
                        stream=response,
                        provider=provider,
                        remaining=ordered[index + 1 :],
                        variants_by_protocol=variants_by_protocol,
                        attempts=attempts,
                        started_at=start,
                        call_type=call_type,
                    )

                latency_ms = (time.perf_counter() - start) * 1000.0
                attempts.append(
                    ProviderAttempt(
                        provider_id=provider.provider_id,
                        provider_name=provider.name,
                        success=True,
                    )
                )
                self._record_attempt_success(
                    provider,
                    call_type=call_type,
                    latency_ms=(latency_ms if call_type is CallType.REGULAR else None),
                    stream_opened=(None if call_type is CallType.REGULAR else False),
                    total_duration_ms=(None if call_type is CallType.REGULAR else latency_ms),
                )
                return response

            if outcome.category in _STOP_CATEGORIES:
                break

        raise RouterExhaustedError(attempts)

    def __enter__(self) -> ProviderRouter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """End this router's life, releasing the metrics store it created.

        Idempotent and terminal, matching RouterStream: invoke() afterwards
        raises RouterClosedError. Only the router-created default store is
        closed -- a caller-provided store is the caller's to close, following
        close-what-you-create. Never interrupts in-flight calls or streams:
        they complete normally, and only bookkeeping that reaches the metrics
        path after close is dropped (visibly counted) so a late write cannot
        reopen the connection this close just released. Finish calls and fully
        drain or close streams before closing to lose nothing.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._owned_metrics_store is not None:
                self._owned_metrics_store.close()

    def reset_health(self, provider_id: str | None = None) -> None:
        """Treat one provider -- or every provider, if given None -- as brand new.

        Clears the cooldown, failure count, auth bench, and last error, so a
        benched provider is eligible again on the next call. Use it when the
        real cause is already fixed (quota upgraded, API key corrected) and
        waiting out the bench serves no purpose. Recorded metrics are never
        touched: MetricsStore has no delete path, so the history of what
        actually happened survives every reset.
        """
        if provider_id is None:
            with self._lock:
                self._health.clear()
            return
        known = sorted(provider.provider_id for provider in self.providers)
        if provider_id not in known:
            # A typo'd reset that quietly did nothing is the exact silent
            # failure this router exists to prevent.
            raise ConfigError(
                f"Cannot reset health for unknown provider ID {provider_id!r}; "
                f"configured provider IDs are: {', '.join(repr(item) for item in known)}."
            )
        with self._lock:
            self._health.pop(provider_id, None)

    def health_report(self) -> dict[str, ProviderHealthReport]:
        """Report every configured provider's health, so a reset can be an informed choice.

        Providers with nothing recorded against them read clean rather than
        going missing. Each report is a frozen copy in a fresh dict, so no live
        router state escapes to the caller.
        """
        now = self._clock()
        report: dict[str, ProviderHealthReport] = {}
        with self._lock:
            for provider in self.providers:
                state = self._health.get(provider.provider_id)
                if state is None:
                    report[provider.provider_id] = ProviderHealthReport(
                        provider_id=provider.provider_id,
                        provider_name=provider.name,
                    )
                    continue
                report[provider.provider_id] = ProviderHealthReport(
                    provider_id=provider.provider_id,
                    provider_name=provider.name,
                    auth_disabled=state.auth_disabled,
                    consecutive_failures=state.consecutive_failures,
                    cooldown_remaining_seconds=state.cooldown_remaining(now),
                    last_error=state.last_error,
                )
        return report

    def _record_attempt_failure(
        self,
        provider: ProviderConfig,
        exc: Exception,
        *,
        call_type: CallType,
        latency_ms: float | None,
        stream_opened: bool | None,
        total_duration_ms: float | None = None,
    ) -> _FailureOutcome:
        """Classify one dead attempt, record it, and bench the provider unless the call is at fault.

        The single copy of the failure rules, shared by invoke()'s loop and
        RouterStream's: a STOP category means the call itself is broken, so the
        provider's health is left untouched -- one malformed request must not
        bench every provider it is tried against.
        """
        category = categorize_error(exc)
        self._record_metrics(
            provider,
            call_type=call_type,
            success=False,
            latency_ms=latency_ms,
            error_type=category.value,
            stream_opened=stream_opened,
            total_duration_ms=total_duration_ms,
        )
        newly_benched = False
        if category not in _STOP_CATEGORIES:
            newly_benched = self._record_failure(provider, category, exc)
        return _FailureOutcome(category=category, newly_benched=newly_benched)

    def _record_attempt_success(
        self,
        provider: ProviderConfig,
        *,
        call_type: CallType,
        latency_ms: float | None,
        stream_opened: bool | None,
        total_duration_ms: float | None = None,
    ) -> None:
        """Record one attempt the provider actually served, in metrics and in health.

        For a stream this runs at the end of the stream, never at its open: a
        provider whose streams open cleanly and die mid-generation would
        otherwise oscillate between failure and success and never bench.
        """
        self._record_metrics(
            provider,
            call_type=call_type,
            success=True,
            latency_ms=latency_ms,
            error_type=None,
            stream_opened=stream_opened,
            total_duration_ms=total_duration_ms,
        )
        self._record_success(provider)

    def _record_failure(
        self, provider: ProviderConfig, category: ErrorCategory, exc: Exception
    ) -> bool:
        """Apply one failure to a provider's health, reporting any bench it starts.

        Get-or-create + mutate, never replace: replacing the state object would
        silently zero an existing failure count.
        """
        with self._lock:
            state = self._health.setdefault(provider.provider_id, ProviderHealthState())
            started_bench = state.record_failure(
                category, str(exc), self._health_config, self._clock()
            )
            if started_bench:
                self._log_bench(provider, state, category)
        return started_bench

    def _should_retry_provider(
        self,
        *,
        provider: ProviderConfig,
        error: Exception,
        outcome: _FailureOutcome,
        attempt_number: int,
        provider_order_index: int,
        call_type: CallType,
        retry_cycle_available: bool,
    ) -> bool:
        """Apply router-owned safety gates, then ask the configured retry policy."""
        policy = self._retry_policy
        ceiling = self._retry_max_attempts
        if policy is None or ceiling is None or not retry_cycle_available:
            return False
        if (
            outcome.category in _NO_RETRY_CATEGORIES
            or outcome.newly_benched
            or attempt_number >= ceiling
        ):
            return False
        retry_context = RetryContext(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            model=provider.model,
            protocol=provider.protocol,
            error=error,
            category=outcome.category,
            attempt_number=attempt_number,
            provider_order_index=provider_order_index,
            is_initial_provider=provider_order_index == 0,
            call_type=call_type,
            stream_opened=False,
            newly_benched=outcome.newly_benched,
        )
        decision = policy.should_retry(retry_context)
        if type(decision) is not bool:
            raise ConfigError(
                "retry_policy.should_retry() must return exactly bool; "
                f"received {type(decision).__name__}"
            )
        return decision

    def _record_success(self, provider: ProviderConfig) -> None:
        """Clear a provider's failure signal, reporting the end of a bench episode."""
        with self._lock:
            state = self._health.get(provider.provider_id)
            if state is None:
                # No entry means nothing to reset; don't create a bogus one.
                return
            was_benched = state.benched
            state.record_success()
        if was_benched:
            logger.info(
                'Provider "%s" (id="%s") recovered; it is no longer benched.',
                provider.name,
                provider.provider_id,
            )

    def _log_bench(
        self, provider: ProviderConfig, state: ProviderHealthState, category: ErrorCategory
    ) -> None:
        """Report a new bench with its real cause; a provider is never benched silently.

        The first bench of an episode warns. Repeat benches within that same
        episode -- a persistently broken provider re-benched once per cooldown
        window -- drop to DEBUG so one outage cannot flood the log. A recovery
        re-arms the warning, so a later, separate outage is reported again
        instead of being buried.
        """
        if category is ErrorCategory.AUTH:
            duration = "for the rest of the run"
            trigger = "after an auth failure"
        elif category is ErrorCategory.RATE_LIMIT:
            duration = f"for {self._health_config.rate_limit_cooldown_seconds:.1f}s"
            trigger = "after rate limiting"
        else:
            duration = f"for {self._health_config.failure_cooldown_seconds:.1f}s"
            trigger = f"after {state.consecutive_failures} consecutive failures"
        logger.log(
            logging.DEBUG if state.warned else logging.WARNING,
            'Benched provider "%s" (id="%s") %s %s; last error: %s',
            provider.name,
            provider.provider_id,
            duration,
            trigger,
            state.last_error,
        )
        state.warned = True

    def _record_metrics(
        self,
        provider: ProviderConfig,
        *,
        call_type: CallType,
        success: bool,
        latency_ms: float | None,
        error_type: str | None,
        stream_opened: bool | None,
        total_duration_ms: float | None = None,
    ) -> None:
        """Persist one MetricsEvent for this attempt; never let storage disturb the call."""
        if self._metrics_store is None:
            return
        event = MetricsEvent(
            metrics_scope=self.metrics_scope,
            provider_id=provider.provider_id,
            provider_name=provider.name,
            model=provider.model,
            protocol=provider.protocol,
            call_type=call_type,
            success=success,
            stream_opened=stream_opened,
            latency_ms=latency_ms,
            error_type=error_type,
            total_duration_ms=total_duration_ms,
        )
        with self._lock:
            if self._closed:
                # A stream draining past close() still reports its outcome
                # here. Dropping the write -- counted, at DEBUG -- is what
                # keeps a closed router closed: reaching the store would
                # lazily reopen the connection close() just released. This is
                # a deliberate close, not a storage outage, so the
                # unavailability warning stays silent.
                self._dropped_metrics_events += 1
                logger.debug("Metrics write after close() dropped.")
                return
            try:
                self._metrics_store.record_attempt(event)
            except Exception:
                self._dropped_metrics_events += 1
                logger.debug("Metrics storage write failed.", exc_info=True)
                if not self._metrics_warning_emitted:
                    logger.warning(
                        "Metrics storage is unavailable (%s); routing will continue, but "
                        "attempts are not being recorded. Enable debug logging for details.",
                        type(self._metrics_store).__name__,
                    )
                    self._metrics_warning_emitted = True
                return

            if self._dropped_metrics_events > 0 and not self._metrics_recovery_emitted:
                logger.info(
                    "Metrics storage recovered after %d unrecorded attempt(s).",
                    self._dropped_metrics_events,
                )
                self._metrics_recovery_emitted = True

    def _log_unrecognized_stream_shape(self, operation: str) -> None:
        """Report once per operation that this stream shape carries no completion marker.

        Repeats drop to DEBUG, following the bench-logging pattern: a provider
        whose chunk shape the adapter cannot read is one fact about that
        operation, not one line per call.
        """
        with self._lock:
            first_time = operation not in self._stream_shape_warned
            self._stream_shape_warned.add(operation)
        logger.log(
            logging.WARNING if first_time else logging.DEBUG,
            "A stream for operation %r ended without any chunk shape this adapter "
            "recognizes; silent-truncation detection is not available for it, so the "
            "stream counts as completed.",
            operation,
        )

    @staticmethod
    def _reject_duplicate_provider_ids(providers: list[ProviderConfig]) -> None:
        """Reject canonical identity collisions while allowing duplicate names."""
        counts = Counter(provider.provider_id for provider in providers)
        duplicates = sorted(provider_id for provider_id, count in counts.items() if count > 1)
        if duplicates:
            raise ConfigError(
                "Duplicate provider ID(s) in configuration: "
                f"{', '.join(repr(provider_id) for provider_id in duplicates)}."
            )

    @staticmethod
    def _validate_metrics_scope(metrics_scope: str) -> str:
        if not isinstance(metrics_scope, str):
            raise ConfigError("metrics_scope must be a string")
        value = metrics_scope.strip()
        if not value:
            raise ConfigError("metrics_scope must not be empty")
        return value

    @staticmethod
    def _validate_metrics_store(metrics_store: object | None) -> MetricsStore | None:
        """Require the complete public structural storage contract at construction."""
        if metrics_store is None:
            return None
        missing_or_noncallable: list[str] = []
        for method_name in ("record_attempt", "query_recent", "query_score_aggregates"):
            try:
                method = getattr(metrics_store, method_name)
            except Exception:
                missing_or_noncallable.append(method_name)
                continue
            if not callable(method):
                missing_or_noncallable.append(method_name)
        if missing_or_noncallable:
            methods = ", ".join(missing_or_noncallable)
            raise ConfigError(
                "metrics_store must implement callable record_attempt, query_recent, and "
                f"query_score_aggregates methods; invalid method(s): {methods}."
            )
        return cast(MetricsStore, metrics_store)

    @staticmethod
    def _arguments_for(variant: CallVariant, provider: ProviderConfig) -> dict[str, object]:
        """Copy the variant's arguments with this provider's model injected.

        A fresh copy per attempt, never a mutation: one CallVariant is reused
        for every provider sharing its protocol, including on a stream restart.
        """
        return {**variant.arguments, "model": provider.model}

    @staticmethod
    def _prepare_variants(
        calls: list[CallVariant],
    ) -> tuple[dict[ApiProtocol, CallVariant], CallType | None]:
        """Validate every CallVariant once, upfront, before any provider is contacted.

        Never mutates a CallVariant's arguments -- the same variant is reused
        across every provider attempt of its protocol in the fallback loop, so
        the model-conflict check runs once here, against the caller's
        original arguments only.
        """
        variants_by_protocol: dict[ApiProtocol, CallVariant] = {}
        for call in calls:
            if call.protocol in variants_by_protocol:
                raise DuplicateCallVariantProtocolError(call.protocol)
            variants_by_protocol[call.protocol] = call
        for variant in variants_by_protocol.values():
            if "model" in variant.arguments:
                raise ModelArgumentConflictError(variant.protocol, variant.operation)
        declared_types = {variant.call_type for variant in variants_by_protocol.values()}
        if len(declared_types) > 1:
            raise MixedCallTypeError(
                [(variant.protocol, variant.call_type) for variant in variants_by_protocol.values()]
            )
        call_type = next(iter(declared_types), None)
        return variants_by_protocol, call_type

    def _adapter_for(self, provider: ProviderConfig) -> ProviderAdapter:
        if not self._using_default_adapters:
            return self._adapter_factory(provider)
        # Check-then-create under the lock: concurrent first calls must not
        # each build an adapter, or the pooled HTTP connections the cache
        # exists to reuse would be duplicated.
        with self._lock:
            adapter = self._default_adapter_cache.get(provider.provider_id)
            if adapter is None:
                adapter = self._adapter_factory(provider)
                self._default_adapter_cache[provider.provider_id] = adapter
        return adapter

    @staticmethod
    def _default_adapter_for(provider: ProviderConfig) -> ProviderAdapter:
        """Map a provider's protocol to its built-in adapter."""
        if provider.protocol == ApiProtocol.OPENAI_CHAT:
            return OpenAICompatibleAdapter(provider)
        if provider.protocol == ApiProtocol.OPENAI_RESPONSES:
            return OpenAIResponsesAdapter(provider)
        # Unreachable via invoke(): unsupported protocols are excluded by the
        # eligibility filter first. Kept as a guard for direct/custom callers.
        raise UnsupportedProtocolError(
            provider.provider_id, provider.name, provider.protocol
        )  # pragma: no cover


class RouterStream:
    """Iterator that keeps the router on the call stack for the life of a stream.

    A stream's real outcome arrives after invoke() has already returned, inside
    the consumer's own loop, where no router code would otherwise be running.
    This object is the only code of ours still on that stack, so it is the only
    possible home for a stream's fallback, metrics and health recording. It
    yields the provider's chunks exactly as they arrive -- nothing buffered,
    nothing accumulated, nothing invented -- and holds the not-yet-tried tail of
    the same ranked provider order invoke() was working through.

    A restart means the next provider regenerates its answer from scratch:
    chunks from two generations cannot be spliced, so whatever the consumer
    accumulated from the dead provider has to be discarded. Never letting that
    happen silently is what ``on_restart`` and ``restarts`` are for.
    """

    def __init__(
        self,
        *,
        router: ProviderRouter,
        stream: NormalizedStream,
        provider: ProviderConfig,
        remaining: list[ProviderConfig],
        variants_by_protocol: dict[ApiProtocol, CallVariant],
        attempts: list[ProviderAttempt],
        started_at: float,
        call_type: CallType,
    ) -> None:
        self._router = router
        self._stream = stream
        self._provider = provider
        self._remaining = list(remaining)
        self._variants_by_protocol = variants_by_protocol
        self._attempts = attempts
        self._started_at = started_at
        self._call_type = call_type
        self._first_chunk_at: float | None = None
        self._chunks_yielded = 0
        self._closed = False
        self.restarts = 0

    def __iter__(self) -> RouterStream:
        return self

    @property
    def usage(self) -> Any:
        """Expose the current provider stream's native usage object, if any."""
        return self._stream.usage

    def __next__(self) -> Any:
        while True:
            if self._closed:
                # Closing is terminal, matching a closed generator: a consumer
                # that stopped early is not handed the rest of its stream.
                raise StopIteration
            failure: Exception
            try:
                chunk = next(self._stream)
            except StopIteration:
                truncation = self._judge_clean_end()
                if truncation is None:
                    raise
                failure = truncation
            except Exception as exc:
                # Already a router error -- that is NormalizedStream's contract.
                failure = exc
            else:
                if self._first_chunk_at is None:
                    self._first_chunk_at = time.perf_counter()
                self._chunks_yielded += 1
                return chunk
            # Deliberately outside the handlers above, so an error raised from
            # here is not chained onto the stream's own StopIteration.
            self._fail(failure)

    def __enter__(self) -> RouterStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """End the stream for good, recording only an outcome the consumer observed.

        Never restarts: a consumer that closed a stream asked for it to stop,
        not for another provider to regenerate it. Idempotent and terminal --
        iterating afterwards raises StopIteration, like a closed generator.

        A completion marker already seen means the call was served, so it is
        recorded like any other success (the break-on-finish_reason pattern
        still feeds scoring). Without one, nothing at all is recorded: the one
        documented exception to one event per attempt, because an outcome the
        caller declined to observe is not one the router can honestly report.
        """
        if self._closed:
            return
        self._closed = True
        self._close_underlying()
        if self._stream.completed:
            self._record_success()

    def _judge_clean_end(self) -> ProviderStreamInterruptedError | None:
        """Judge a stream that ended without error: None if it truly finished.

        A stream that yielded nothing did not serve a usable response, whatever
        completion state its wrapper reports. Otherwise, a stream finished iff
        the provider marked it finished. The exception is a stream whose chunk
        shape the adapter never recognized: its completion marker was never
        readable in the first place, and the router does not invent a failure
        it cannot evidence.
        """
        if self._chunks_yielded == 0:
            return ProviderStreamInterruptedError(
                f'Provider "{self._provider.name}" (id="{self._provider.provider_id}") '
                f"ended its stream for model "
                f"{self._provider.model!r} without yielding any chunks; no usable "
                f"response was produced.",
                provider_id=self._provider.provider_id,
                provider_name=self._provider.name,
                model=self._provider.model,
            )
        if not self._stream.completed:
            if self._stream.recognized:
                return ProviderStreamInterruptedError(
                    f'Provider "{self._provider.name}" (id="{self._provider.provider_id}") '
                    f"ended its stream for model "
                    f"{self._provider.model!r} after {self._chunks_yielded} chunk(s) without "
                    f"ever marking it complete; the response was silently truncated.",
                    provider_id=self._provider.provider_id,
                    provider_name=self._provider.name,
                    model=self._provider.model,
                )
            self._router._log_unrecognized_stream_shape(self._operation())
        self._record_success()
        self._close_underlying()
        self._closed = True
        return None

    def _fail(self, error: Exception) -> None:
        """Record the attempt this error killed, then restart on the next provider or raise."""
        duration_ms = self._duration_ms()
        self._close_underlying()
        self._attempts.append(
            ProviderAttempt(
                provider_id=self._provider.provider_id,
                provider_name=self._provider.name,
                success=False,
                error=error,
            )
        )
        outcome = self._router._record_attempt_failure(
            self._provider,
            error,
            call_type=self._call_type,
            latency_ms=self._ttft_ms(),
            stream_opened=True,
            total_duration_ms=duration_ms,
        )
        logger.warning(
            'Provider "%s" (id="%s") stream died after %d chunk(s) and %.1fms: %s',
            self._provider.name,
            self._provider.provider_id,
            self._chunks_yielded,
            duration_ms,
            error,
        )
        if (
            outcome.category in _STOP_CATEGORIES
            or self._router._stream_failure_policy is StreamFailurePolicy.RAISE
        ):
            # Both stop here, and both hand the consumer the provider's own
            # error rather than the router's summary of it.
            self._closed = True
            raise error
        self._restart(error)

    def _restart(self, error: Exception) -> None:
        """Open the next provider in the ranked order, or exhaust trying.

        Each remaining ordered occurrence is consumed once and the list is
        never refilled. PR27 retry counters do not enter RouterStream, so no
        same-provider retry cycle is added after a stream opens; there is no
        separate max-restarts knob.
        """
        while self._remaining:
            provider = self._remaining.pop(0)
            variant = self._variants_by_protocol[provider.protocol]
            arguments = self._router._arguments_for(variant, provider)
            start = time.perf_counter()
            try:
                response = self._router._adapter_for(provider).invoke(variant.operation, arguments)
            except Exception as exc:
                latency_ms = (time.perf_counter() - start) * 1000.0
                self._attempts.append(
                    ProviderAttempt(
                        provider_id=provider.provider_id,
                        provider_name=provider.name,
                        success=False,
                        error=exc,
                    )
                )
                outcome = self._router._record_attempt_failure(
                    provider,
                    exc,
                    call_type=self._call_type,
                    latency_ms=(latency_ms if self._call_type is CallType.REGULAR else None),
                    stream_opened=(None if self._call_type is CallType.REGULAR else False),
                    total_duration_ms=(None if self._call_type is CallType.REGULAR else latency_ms),
                )
                if outcome.category in _STOP_CATEGORIES:
                    break
                continue

            if not isinstance(response, NormalizedStream):
                latency_ms = (time.perf_counter() - start) * 1000.0
                self._reject_non_stream(provider, response, latency_ms=latency_ms)
                continue

            self._announce_restart(error, provider)
            self._stream = response
            self._provider = provider
            self._started_at = start
            self._first_chunk_at = None
            self._chunks_yielded = 0
            return

        self._closed = True
        raise RouterExhaustedError(self._attempts)

    def _reject_non_stream(
        self, provider: ProviderConfig, response: Any, *, latency_ms: float
    ) -> None:
        """Discard a restart that came back as a whole response instead of a stream.

        Defensive: a consumer part-way through iterating chunks cannot be handed
        a single response object, and splicing one into a stream in progress
        would be worse than falling back again.
        """
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Closing a non-stream restart response failed.", exc_info=True)
        anomaly = ProviderError(
            f'Provider "{provider.name}" (id="{provider.provider_id}") returned a non-streaming '
            f"{type(response).__name__} while restarting a stream for model "
            f"{provider.model!r}; it cannot be spliced into a stream already in progress.",
            provider_id=provider.provider_id,
            provider_name=provider.name,
            model=provider.model,
        )
        self._attempts.append(
            ProviderAttempt(
                provider_id=provider.provider_id,
                provider_name=provider.name,
                success=False,
                error=anomaly,
            )
        )
        self._router._record_attempt_failure(
            provider,
            anomaly,
            call_type=self._call_type,
            latency_ms=(latency_ms if self._call_type is CallType.REGULAR else None),
            stream_opened=(None if self._call_type is CallType.REGULAR else False),
            total_duration_ms=(None if self._call_type is CallType.REGULAR else latency_ms),
        )

    def _announce_restart(self, error: Exception, provider: ProviderConfig) -> None:
        """Make a restart visible before the next provider starts regenerating.

        ``restarts`` counts every restart. The callback and the warning fire
        only once chunks have been yielded: a restart at zero chunks leaves the
        consumer nothing to discard, so there is nothing to warn about. With
        chunks already out and no callback registered, that warning is all that
        stands between the consumer and silently corrupted accumulated output.
        """
        self.restarts += 1
        if not self._chunks_yielded:
            return
        if self._router._on_restart is None:
            logger.warning(
                'Discarding %d chunk(s) already yielded by provider "%s" (id="%s") and '
                'restarting on provider "%s" (id="%s"), which regenerates from scratch: '
                "%s. Register on_restart to "
                "handle this in code.",
                self._chunks_yielded,
                self._provider.name,
                self._provider.provider_id,
                provider.name,
                provider.provider_id,
                error,
            )
            return
        # A callback's own exception propagates: the router does not decide that
        # a consumer's restart handling failing is survivable.
        self._router._on_restart(
            StreamRestart(
                failed_provider=self._provider.name,
                failed_provider_id=self._provider.provider_id,
                error=error,
                next_provider=provider.name,
                next_provider_id=provider.provider_id,
                chunks_yielded=self._chunks_yielded,
                restart_count=self.restarts,
            )
        )

    def _record_success(self) -> None:
        """Record the current attempt as served.

        Both callers close the stream immediately afterwards, and a closed
        stream records nothing further, so one attempt cannot be recorded twice.
        """
        self._attempts.append(
            ProviderAttempt(
                provider_id=self._provider.provider_id,
                provider_name=self._provider.name,
                success=True,
            )
        )
        self._router._record_attempt_success(
            self._provider,
            call_type=self._call_type,
            latency_ms=self._ttft_ms(),
            stream_opened=True,
            total_duration_ms=self._duration_ms(),
        )

    def _close_underlying(self) -> None:
        """Release the provider's connection; failing to must not mask the real outcome."""
        try:
            self._stream.close()
        except Exception:
            logger.debug("Closing the provider stream failed.", exc_info=True)

    def _operation(self) -> str:
        return self._variants_by_protocol[self._provider.protocol].operation

    def _ttft_ms(self) -> float | None:
        """Time to this attempt's first chunk, or None if no chunk ever arrived."""
        if self._first_chunk_at is None:
            return None
        return (self._first_chunk_at - self._started_at) * 1000.0

    def _duration_ms(self) -> float:
        """This attempt's span so far -- to completion, or to the death of its stream."""
        return (time.perf_counter() - self._started_at) * 1000.0
