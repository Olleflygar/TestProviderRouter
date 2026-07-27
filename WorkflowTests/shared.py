from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

WORKFLOW_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = WORKFLOW_ROOT.parent
ROUTER_SRC = PROJECT_ROOT / "ProviderRouterPR1" / "src"
DATABASE_PATH = WORKFLOW_ROOT / "workflow_history.duckdb"
DEFAULT_TOPIC = "Why short breaks can help people stay focused"
LOOKBACK_HOURS = 336.0

# Keep the scripts runnable from an IDE without installing the local package.
sys.path.insert(0, str(ROUTER_SRC))

from nygen_router import (  # noqa: E402
    ApiProtocol,
    CallVariant,
    DuckDBMetricsStore,
    MetricsEvent,
    ProviderConfig,
    ProviderRouter,
    RoundRobinPolicy,
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
    provider_name: str | None
    attempts: tuple[MetricsEvent, ...]


def parse_options(description: str) -> WorkflowOptions:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--topic",
        default=DEFAULT_TOPIC,
        help=f"Simple topic processed by the four workflow steps (default: {DEFAULT_TOPIC!r}).",
    )
    parser.add_argument(
        "--reset-history",
        action="store_true",
        help="Delete the shared workflow DuckDB before calibration.",
    )
    args = parser.parse_args()
    return WorkflowOptions(topic=args.topic, reset_history=args.reset_history)


def load_project_environment() -> None:
    """Load the same project-root .env file as the existing usage scripts."""
    load_dotenv(PROJECT_ROOT / ".env")


def provider_configs() -> list[ProviderConfig]:
    """Return the two provider/model pairs used by UsageTestRoundRobin.py."""
    return [
        ProviderConfig(
            name="Fireworks",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="accounts/fireworks/models/gpt-oss-20b",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="Fireworks_API_KEY",
        ),
        ProviderConfig(
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
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("Provider API key configuration is incomplete:\n- " + "\n- ".join(errors))


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
    providers: list[ProviderConfig], store: DuckDBMetricsStore
) -> ProviderRouter:
    return ProviderRouter(
        providers=providers,
        policy=ScoreBasedPolicy(
            weights=SCORE_WEIGHTS,
            lookback_hours=LOOKBACK_HOURS,
            use_streaming=False,
        ),
        metrics_store=store,
    )


def run_calibration(
    providers: list[ProviderConfig],
    store: DuckDBMetricsStore,
    *,
    rounds: int = 2,
) -> None:
    """Give each provider two chances to lead before score-based routing starts."""
    router = ProviderRouter(
        providers=providers,
        policy=RoundRobinPolicy(),
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
            )
    print_score_snapshot(providers, store, heading="Scores after calibration")


def invoke_regular(
    router: ProviderRouter,
    store: DuckDBMetricsStore,
    *,
    prompt: str,
    label: str,
    max_tokens: int,
    require_text: bool = True,
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
        _print_attempts(label, attempts)

    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        if require_text:
            raise RuntimeError(f"{label} returned no text content.")
        content = ""

    successful = [event for event in attempts if event.success]
    provider_name = successful[-1].provider_name if successful else None
    return RouterResult(
        text=content.strip(),
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
    events = store.query_recent(since=since)
    stats_by_provider = aggregate_stats(events, [provider.name for provider in providers])

    print(f"\n{heading}")
    print(
        "  provider    attempts  success rate  avg latency  "
        "success score  speed score  total"
    )
    for provider in providers:
        stats = stats_by_provider[provider.name]
        score = calculate_provider_score(stats, SCORE_WEIGHTS, use_streaming=False)
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


def _events_since(store: DuckDBMetricsStore, since: datetime) -> list[MetricsEvent]:
    try:
        return store.query_recent(since=since)
    except Exception as exc:
        print(f"Could not read diagnostics from DuckDB: {exc}")
        return []


def _print_attempts(label: str, attempts: list[MetricsEvent]) -> None:
    print(f"\n[{label}]")
    if not attempts:
        print("  No persisted provider attempt was found.")
        return
    for event in attempts:
        outcome = "success" if event.success else f"failed ({event.error_type or 'unknown'})"
        latency = "n/a" if event.latency_ms is None else f"{event.latency_ms:.0f} ms"
        print(f"  {event.provider_name}: {outcome}, latency={latency}, stream={event.stream}")
