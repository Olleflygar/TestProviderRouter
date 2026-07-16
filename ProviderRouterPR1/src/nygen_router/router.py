from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any

from nygen_router.adapters.base import ProviderAdapter
from nygen_router.adapters.openai_compatible import OpenAICompatibleAdapter
from nygen_router.config import ApiProtocol, ProviderConfig
from nygen_router.errors import (
    ConfigError,
    DuplicateCallVariantProtocolError,
    ErrorCategory,
    ModelArgumentConflictError,
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
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
from nygen_router.policies import Policy, RoundRobinPolicy
from nygen_router.storage.base import MetricsStore
from nygen_router.storage.duckdb import DuckDBMetricsStore
from nygen_router.types import CallVariant, ProviderAttempt

AdapterFactory = Callable[[ProviderConfig], ProviderAdapter]

logger = logging.getLogger(__name__)

# Protocols the built-in adapter factory can serve. Adding a new adapter
# (e.g. OPENAI_RESPONSES in PR12) means registering its protocol here so the
# eligibility filter stops excluding it.
SUPPORTED_PROTOCOLS = frozenset({ApiProtocol.OPENAI_CHAT})

# Failure categories that abort the whole run immediately instead of falling
# back to the next eligible provider: the call itself is broken (malformed
# request, bad operation, mismatched arguments, missing SDK), so no other
# provider trying the same broken call would fare any better.
_STOP_CATEGORIES = frozenset({ErrorCategory.BAD_REQUEST, ErrorCategory.INVALID_OPERATION})


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
    ):
        self.providers = list(providers)
        self._adapter_factory = adapter_factory or self._default_adapter_for
        self._policy = policy or RoundRobinPolicy()
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
        self._metrics_store: MetricsStore | None = (
            DuckDBMetricsStore() if isinstance(metrics_store, _UnsetType) else metrics_store
        )
        # A missing DuckDB dependency was already reported by the store at
        # construction, so the first failed write must not repeat the warning.
        self._metrics_warning_emitted = (
            isinstance(self._metrics_store, DuckDBMetricsStore)
            and not self._metrics_store.available
        )
        self._metrics_recovery_emitted = False
        self._dropped_metrics_events = 0

    def invoke(self, calls: list[CallVariant]) -> Any:
        """Filter, order eligible providers, then try them in turn with fallback.

        Returns the raw native response object the winning provider's SDK
        returned -- untouched, with nothing attached. Every attempt and
        exclusion is still tracked internally, so a total failure still
        raises an error enumerating each provider's own real reason.
        """
        if not self.providers:
            raise NoProvidersConfiguredError("No providers configured.")

        variants_by_protocol = self._prepare_variants(calls)

        eligible, excluded = filter_eligible_providers(
            self.providers,
            supported_protocols=self._supported_protocols,
            requested_protocols=variants_by_protocol.keys(),
            health=self._health,
            now=self._clock(),
        )
        if not eligible:
            raise NoEligibleProvidersError(excluded)

        attempts: list[ProviderAttempt] = []
        for provider in self._policy.order(eligible):
            variant = variants_by_protocol[provider.protocol]
            arguments = {**variant.arguments, "model": provider.model}
            adapter = self._adapter_for(provider)
            start = time.perf_counter()
            try:
                response = adapter.invoke(variant.operation, arguments)
            except Exception as exc:
                latency_ms = (time.perf_counter() - start) * 1000.0
                attempts.append(
                    ProviderAttempt(provider_name=provider.name, success=False, error=exc)
                )
                category = categorize_error(exc)
                self._record_metrics(
                    provider, success=False, latency_ms=latency_ms, error_type=category.value
                )
                if category in _STOP_CATEGORIES:
                    # The call is at fault, not the provider: leave its health
                    # untouched and stop before any of it is blamed.
                    break
                self._record_failure(provider.name, category, exc)
                continue

            latency_ms = (time.perf_counter() - start) * 1000.0
            attempts.append(ProviderAttempt(provider_name=provider.name, success=True))
            self._record_metrics(provider, success=True, latency_ms=latency_ms, error_type=None)
            self._record_success(provider.name)
            return response

        raise RouterExhaustedError(attempts)

    def reset_health(self, provider_name: str | None = None) -> None:
        """Treat one provider -- or every provider, if given None -- as brand new.

        Clears the cooldown, failure count, auth bench, and last error, so a
        benched provider is eligible again on the next call. Use it when the
        real cause is already fixed (quota upgraded, API key corrected) and
        waiting out the bench serves no purpose. Recorded metrics are never
        touched: MetricsStore has no delete path, so the history of what
        actually happened survives every reset.
        """
        if provider_name is None:
            self._health.clear()
            return
        known = sorted(provider.name for provider in self.providers)
        if provider_name not in known:
            # A typo'd reset that quietly did nothing is the exact silent
            # failure this router exists to prevent.
            raise ConfigError(
                f"Cannot reset health for unknown provider {provider_name!r}; "
                f"configured providers are: {', '.join(repr(name) for name in known)}."
            )
        self._health.pop(provider_name, None)

    def health_report(self) -> dict[str, ProviderHealthReport]:
        """Report every configured provider's health, so a reset can be an informed choice.

        Providers with nothing recorded against them read clean rather than
        going missing. Each report is a frozen copy in a fresh dict, so no live
        router state escapes to the caller.
        """
        now = self._clock()
        report: dict[str, ProviderHealthReport] = {}
        for provider in self.providers:
            state = self._health.get(provider.name)
            if state is None:
                report[provider.name] = ProviderHealthReport()
                continue
            report[provider.name] = ProviderHealthReport(
                auth_disabled=state.auth_disabled,
                consecutive_failures=state.consecutive_failures,
                cooldown_remaining_seconds=state.cooldown_remaining(now),
                last_error=state.last_error,
            )
        return report

    def _record_failure(self, provider_name: str, category: ErrorCategory, exc: Exception) -> None:
        """Apply one failure to a provider's health, reporting any bench it starts.

        Get-or-create + mutate, never replace: replacing the state object would
        silently zero an existing failure count.
        """
        state = self._health.setdefault(provider_name, ProviderHealthState())
        started_bench = state.record_failure(category, str(exc), self._health_config, self._clock())
        if started_bench:
            self._log_bench(provider_name, state, category)

    def _record_success(self, provider_name: str) -> None:
        """Clear a provider's failure signal, reporting the end of a bench episode."""
        state = self._health.get(provider_name)
        if state is None:
            # No entry means nothing to reset; don't create a bogus one.
            return
        was_benched = state.benched
        state.record_success()
        if was_benched:
            logger.info("Provider %r recovered; it is no longer benched.", provider_name)

    def _log_bench(
        self, provider_name: str, state: ProviderHealthState, category: ErrorCategory
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
            "Benched provider %r %s %s; last error: %s",
            provider_name,
            duration,
            trigger,
            state.last_error,
        )
        state.warned = True

    def _record_metrics(
        self,
        provider: ProviderConfig,
        *,
        success: bool,
        latency_ms: float,
        error_type: str | None,
    ) -> None:
        """Persist one MetricsEvent for this attempt; never let storage disturb the call."""
        if self._metrics_store is None:
            return
        event = MetricsEvent(
            provider_name=provider.name,
            model=provider.model,
            protocol=provider.protocol,
            success=success,
            latency_ms=latency_ms,
            error_type=error_type,
        )
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

    @staticmethod
    def _prepare_variants(calls: list[CallVariant]) -> dict[ApiProtocol, CallVariant]:
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
        return variants_by_protocol

    def _adapter_for(self, provider: ProviderConfig) -> ProviderAdapter:
        return self._adapter_factory(provider)

    @staticmethod
    def _default_adapter_for(provider: ProviderConfig) -> ProviderAdapter:
        """Map a provider's protocol to its adapter (only OPENAI_CHAT exists so far)."""
        if provider.protocol == ApiProtocol.OPENAI_CHAT:
            return OpenAICompatibleAdapter(provider)
        # Unreachable via invoke(): unsupported protocols are excluded by the
        # eligibility filter first. Kept as a guard for direct/custom callers.
        raise UnsupportedProtocolError(provider.name, provider.protocol)  # pragma: no cover
