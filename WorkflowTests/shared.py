from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

WORKFLOW_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = WORKFLOW_ROOT.parent
ROUTER_SRC = PROJECT_ROOT / "ProviderRouter" / "src"
DATABASE_PATH = WORKFLOW_ROOT / "workflow_history.duckdb"
DEFAULT_TOPIC = "Why short breaks can help people stay focused"
LOOKBACK_HOURS = 336.0
METRICS_SCOPE = "workflow-tests:local"

# Keep the scripts runnable from an IDE without installing the local package.
sys.path.insert(0, str(ROUTER_SRC))

from nygen_router import (  # noqa: E402
    ApiProtocol,
    CallType,
    CallVariant,
    DuckDBMetricsStore,
    MetricsEvent,
    MissingApiKeyError,
    Policy,
    ProviderConfig,
    ProviderRouter,
    RetryContext,
    RetryPolicy,
    RoundRobinPolicy,
    RoutingContext,
    ScoreBasedPolicy,
    ScoreWeights,
    aggregate_stats,
    calculate_provider_score,
)

SCORE_WEIGHTS = ScoreWeights()


@dataclass(frozen=True)
class WorkflowOptions:
    topic: str
    reset_history: bool


@dataclass(frozen=True)
class RouterResult:
    text: str
    provider_id: str | None
    provider_name: str | None
    attempts: tuple[MetricsEvent, ...]


class DecisionPrintingPolicy:
    """Show the eligible providers and the order chosen by another policy."""

    def __init__(self, policy: Policy, *, name: str, concise: bool = False) -> None:
        self._policy = policy
        self._name = name
        self._concise = concise

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        ordered = self._policy.order(eligible, context)
        if self._concise:
            names = " -> ".join(provider.name for provider in ordered)
            print(f"\n[routing] {self._name}: {names}")
            return ordered

        print(f"\nRouting decision: {self._name}")
        print(f"  Call type: {context.call_type.value}")
        print("  Eligible: " + " -> ".join(provider.name for provider in eligible))
        print("  Attempt order: " + " -> ".join(provider.name for provider in ordered))
        if ordered:
            print(f"  Decision: try {ordered[0].name} first; use the rest as fallbacks.")
        return ordered


class DecisionPrintingRetryPolicy:
    """Show each decision made by another retry policy."""

    def __init__(self, policy: RetryPolicy, *, concise: bool = False) -> None:
        self._policy = policy
        self._concise = concise

    @property
    def max_attempts(self) -> int:
        return self._policy.max_attempts

    def should_retry(self, context: RetryContext) -> bool:
        retry = self._policy.should_retry(context)
        if self._concise:
            decision = f"retry {context.provider_name}" if retry else "continue the fallback order"
            print(
                f"\n[retry] {context.provider_name} attempt {context.attempt_number}: "
                f"{context.category.value} -> {decision}"
            )
            return retry

        print("\nRetry decision")
        print(
            f"  {context.provider_name} attempt {context.attempt_number} failed "
            f"with {context.category.value}."
        )
        if retry:
            print(f"  Decision: retry {context.provider_name} immediately.")
        else:
            print("  Decision: stop retrying this provider and continue the fallback order.")
        return retry


def parse_options(description: str) -> WorkflowOptions:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--topic",
        default=DEFAULT_TOPIC,
        help=f"Simple topic processed by the workflow (default: {DEFAULT_TOPIC!r}).",
    )
    parser.add_argument(
        "--reset-history",
        action="store_true",
        help="Delete the shared workflow DuckDB before the run.",
    )
    args = parser.parse_args()
    return WorkflowOptions(topic=args.topic, reset_history=args.reset_history)


def load_project_environment() -> None:
    """Load the same project-root .env file as the existing usage scripts."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def provider_configs() -> list[ProviderConfig]:
    """Return the two provider/model pairs used by UsageTestRoundRobin.py."""
    return [
        ProviderConfig(
            provider_id="fireworks:gpt-oss-20b",
            name="Fireworks",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="accounts/fireworks/models/gpt-oss-20b",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="Fireworks_API_KEY",
        ),
        ProviderConfig(
            provider_id="together:gpt-oss-20b",
            name="TogetherAI",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="OpenAI/gpt-oss-20B",
            base_url="https://api.together.ai/v1",
            api_key_env="Together_API_KEY",
        ),
    ]


def require_api_keys(providers: list[ProviderConfig]) -> None:
    """Fail before making calls when either user-managed key is missing."""
    errors: list[str] = []
    for provider in providers:
        try:
            provider.resolve_api_key()
        except MissingApiKeyError as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError(
            "Provider API key configuration is incomplete:\n- " + "\n- ".join(errors)
        )


def open_metrics_store(*, reset_history: bool) -> DuckDBMetricsStore:
    if reset_history:
        DATABASE_PATH.unlink(missing_ok=True)
        Path(f"{DATABASE_PATH}.wal").unlink(missing_ok=True)
        print(f"Reset shared metrics history: {DATABASE_PATH}")

    store = DuckDBMetricsStore(DATABASE_PATH)
    if not store.available:
        raise RuntimeError(
            "DuckDB is required for these workflows. Install WorkflowTests/requirements.txt."
        )
    print(f"Using shared metrics history: {DATABASE_PATH}")
    return store


def score_based_router(
    providers: list[ProviderConfig],
    store: DuckDBMetricsStore,
    *,
    print_decisions: bool = False,
) -> ProviderRouter:
    policy: Policy = ScoreBasedPolicy(
        weights=SCORE_WEIGHTS,
        lookback_hours=LOOKBACK_HOURS,
    )
    if print_decisions:
        policy = DecisionPrintingPolicy(policy, name="score-based policy", concise=True)
    return ProviderRouter(
        providers=providers,
        metrics_scope=METRICS_SCOPE,
        policy=policy,
        metrics_store=store,
    )


def run_calibration(
    providers: list[ProviderConfig],
    store: DuckDBMetricsStore,
    *,
    rounds: int = 2,
    print_decisions: bool = False,
    concise_output: bool = False,
    print_scores: bool = True,
) -> None:
    """Give each provider two chances to lead before score-based routing starts."""
    policy: Policy = RoundRobinPolicy()
    if print_decisions:
        policy = DecisionPrintingPolicy(
            policy,
            name="round-robin calibration",
            concise=concise_output,
        )
    router = ProviderRouter(
        providers=providers,
        metrics_scope=METRICS_SCOPE,
        policy=policy,
        metrics_store=store,
    )
    print(f"\nCalibration: {rounds} round-robin rounds ({rounds * len(providers)} calls)")
    for round_number in range(1, rounds + 1):
        for call_number in range(1, len(providers) + 1):
            invoke_regular(
                router,
                store,
                prompt="Reply with only the word OK.",
                label=f"calibration {round_number}.{call_number}",
                max_tokens=128,
                require_text=False,
                concise_output=concise_output,
            )
    if print_scores:
        print_score_snapshot(providers, store, heading="Scores after calibration")


def invoke_regular(
    router: ProviderRouter,
    store: DuckDBMetricsStore,
    *,
    prompt: str,
    label: str,
    max_tokens: int,
    require_text: bool = True,
    concise_output: bool = False,
) -> RouterResult:
    """Make one explicitly non-streaming call and report its provider attempts."""
    started_at = datetime.now(UTC)
    response = None
    try:
        response = router.invoke(
            [
                CallVariant(
                    protocol=ApiProtocol.OPENAI_CHAT,
                    operation="chat.completions.create",
                    call_type=CallType.REGULAR,
                    arguments={
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "reasoning_effort": "low",
                        "stream": False,
                    },
                )
            ]
        )
    finally:
        attempts = _events_since(store, started_at)
        _print_attempts(label, attempts, concise=concise_output)

    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        if require_text:
            raise RuntimeError(f"{label} returned no text content.")
        content = ""

    successful = [event for event in attempts if event.success]
    provider_id = successful[-1].provider_id if successful else None
    provider_name = successful[-1].provider_name if successful else None
    return RouterResult(
        text=content.strip(),
        provider_id=provider_id,
        provider_name=provider_name,
        attempts=tuple(attempts),
    )


def print_score_snapshot(
    providers: list[ProviderConfig],
    store: DuckDBMetricsStore,
    *,
    heading: str,
) -> None:
    since = datetime.now(UTC) - timedelta(hours=LOOKBACK_HOURS)
    events = store.query_recent(since=since, metrics_scope=METRICS_SCOPE)
    stats_by_provider = aggregate_stats(events, providers, CallType.REGULAR)

    print(f"\n{heading}")
    print("  provider    attempts  success rate  avg latency  success score  speed score  total")
    for provider in providers:
        stats = stats_by_provider[provider.provider_id]
        score = calculate_provider_score(stats, SCORE_WEIGHTS, call_type=CallType.REGULAR)
        success_rate = (
            "n/a" if stats.regular_success_rate is None else f"{stats.regular_success_rate:.1%}"
        )
        latency = (
            "n/a"
            if stats.regular_avg_latency_ms is None
            else f"{stats.regular_avg_latency_ms:.0f} ms"
        )
        print(
            f"  {provider.name:<11} {stats.regular_attempt_count:>8.0f}  "
            f"{success_rate:>12}  {latency:>11}  "
            f"{score.success_quality:>13.3f}  {score.speed_quality:>11.3f}  "
            f"{score.total:>5.3f}"
        )


def print_aggregate_history_table(
    providers: list[ProviderConfig],
    store: DuckDBMetricsStore,
    *,
    heading: str = "Shared DuckDB averages across all runs",
) -> None:
    """Compare providers using every matching regular-call row in shared history."""
    events = _events_since(store, datetime(1970, 1, 1, tzinfo=UTC))
    stats_by_provider = aggregate_stats(events, providers, CallType.REGULAR)
    print(f"\n{heading}")
    print(f"  {DATABASE_PATH}")
    print(f"  Pooled from {len(events)} recorded attempts in scope {METRICS_SCOPE!r}.")

    headers = (
        "provider",
        "attempts",
        "successes",
        "success rate",
        "avg success latency",
        "errors",
        "rate limits",
        "timeouts",
        "success score",
        "speed score",
        "total score",
    )
    rows: list[tuple[str, ...]] = []
    for provider in providers:
        stats = stats_by_provider[provider.provider_id]
        score = calculate_provider_score(stats, SCORE_WEIGHTS, call_type=CallType.REGULAR)
        success_rate = (
            "n/a" if stats.regular_success_rate is None else f"{stats.regular_success_rate:.1%}"
        )
        latency = (
            "n/a"
            if stats.regular_avg_latency_ms is None
            else f"{stats.regular_avg_latency_ms:.0f} ms"
        )
        rows.append(
            (
                provider.name,
                f"{stats.regular_attempt_count:.0f}",
                f"{stats.regular_success_count:.0f}",
                success_rate,
                latency,
                str(stats.recent_error_count),
                str(stats.rate_limit_count),
                str(stats.timeout_count),
                f"{score.success_quality:.3f}",
                f"{score.speed_quality:.3f}",
                f"{score.total:.3f}",
            )
        )
    _print_table(headers, rows)


def print_history_table(
    store: DuckDBMetricsStore,
    *,
    since: datetime,
    heading: str = "DuckDB history for this run",
) -> None:
    """Print persisted provider-attempt rows recorded since the run began."""
    events = _events_since(store, since)
    print(f"\n{heading}")
    print(f"  Database: {DATABASE_PATH}")
    print(f"  Metrics scope: {METRICS_SCOPE}")
    if not events:
        print("  No rows were recorded.")
        return

    headers = (
        "#",
        "time (UTC)",
        "provider",
        "provider ID",
        "protocol",
        "type",
        "outcome",
        "latency",
        "error",
    )
    rows = [
        (
            str(index),
            event.timestamp.astimezone(UTC).strftime("%H:%M:%S.%f")[:-3],
            event.provider_name,
            event.provider_id,
            event.protocol.value,
            event.call_type.value,
            "success" if event.success else "failed",
            "n/a" if event.latency_ms is None else f"{event.latency_ms:.0f} ms",
            event.error_type or "-",
        )
        for index, event in enumerate(events, start=1)
    ]
    _print_table(headers, rows)


def _print_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    """Render a small dependency-free terminal table."""
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    separator = "  +" + "+".join("-" * (width + 2) for width in widths) + "+"

    def render(row: tuple[str, ...]) -> str:
        cells = [f" {value:<{widths[index]}} " for index, value in enumerate(row)]
        return "  |" + "|".join(cells) + "|"

    print(separator)
    print(render(headers))
    print(separator)
    for row in rows:
        print(render(row))
    print(separator)


def _events_since(store: DuckDBMetricsStore, since: datetime) -> list[MetricsEvent]:
    try:
        return store.query_recent(since=since, metrics_scope=METRICS_SCOPE)
    except Exception as exc:
        print(f"Could not read diagnostics from DuckDB: {exc}")
        return []


def _print_attempts(label: str, attempts: list[MetricsEvent], *, concise: bool = False) -> None:
    print(f"\n[{label}]")
    if not attempts:
        print("  No persisted provider attempt was found.")
        return
    for attempt_number, event in enumerate(attempts, start=1):
        outcome = "success" if event.success else f"failed ({event.error_type or 'unknown'})"
        latency = "n/a" if event.latency_ms is None else f"{event.latency_ms:.0f} ms"
        if concise:
            print(
                f"  attempt {attempt_number}: {event.provider_name} | {outcome} | latency {latency}"
            )
            continue
        print(
            f"  {event.provider_name} ({event.provider_id}): {outcome}, "
            f"latency={latency}, call_type={event.call_type.value}, "
            f"stream_opened={event.stream_opened}"
        )
    if concise:
        successful = [event for event in attempts if event.success]
        if successful:
            print(f"  chosen: {successful[-1].provider_name}")
