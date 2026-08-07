"""PR31 baseline thread safety: the stores and one shared router across threads.

Exact counting is the honest observable for lost-update races: with correct
locking, N threads times M operations leave exactly N*M rows (or an exact
round-robin split), while a race silently loses some. The cross-thread SQLite
test is the red-then-green proof for PR31's check_same_thread change: before
it, sqlite3 raised ProgrammingError from any second thread.
"""

from __future__ import annotations

import importlib.util
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_provider_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    DuckDBMetricsStore,
    MetricsEvent,
    ProviderConfig,
    ProviderRouter,
    SQLiteMetricsStore,
)

_DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None
requires_duckdb = pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed")

_SINCE = datetime(2000, 1, 1, tzinfo=UTC)


def _config(name: str) -> ProviderConfig:
    return ProviderConfig(
        provider_id=name,
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
    )


def _calls() -> list[CallVariant]:
    return [
        CallVariant(
            call_type=CallType.REGULAR,
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={"messages": [{"role": "user", "content": "hi"}]},
        )
    ]


class _EchoAdapter:
    """Always succeeds instantly, echoing back which provider served the call."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    def invoke(self, operation: str, arguments: dict[str, object]) -> str:
        return self.config.name


def _event(provider_id: str = "provider_a") -> MetricsEvent:
    return MetricsEvent(
        metrics_scope="test",
        provider_id=provider_id,
        provider_name=provider_id,
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        call_type=CallType.REGULAR,
        success=True,
    )


def _run_threads(worker: Callable[[], None], count: int) -> list[Exception]:
    """Release count threads through one barrier at once and surface their errors."""
    barrier = threading.Barrier(count)
    errors: list[Exception] = []

    def run() -> None:
        try:
            barrier.wait()
            worker()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_sqlite_store_is_usable_from_a_second_thread(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite")
    # Opens the connection on the main thread, so the worker below exercises
    # genuine cross-thread reuse of that same connection.
    store.record_attempt(_event())

    errors = _run_threads(lambda: store.record_attempt(_event()), 1)

    assert errors == []
    assert len(store.query_recent(since=_SINCE)) == 2
    store.close()


def test_sqlite_store_concurrent_writes_lose_nothing(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite")
    threads, writes = 8, 25

    def worker() -> None:
        for _ in range(writes):
            store.record_attempt(_event())

    errors = _run_threads(worker, threads)

    assert errors == []
    assert len(store.query_recent(since=_SINCE)) == threads * writes
    store.close()


@requires_duckdb
def test_duckdb_store_concurrent_writes_lose_nothing(tmp_path: Path) -> None:
    store = DuckDBMetricsStore(tmp_path / "metrics.duckdb")
    threads, writes = 8, 25

    def worker() -> None:
        for _ in range(writes):
            store.record_attempt(_event())

    errors = _run_threads(worker, threads)

    assert errors == []
    assert len(store.query_recent(since=_SINCE)) == threads * writes
    store.close()


def test_one_shared_router_across_threads_records_every_attempt(tmp_path: Path) -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite")
    router = ProviderRouter(
        metrics_scope="test",
        providers=providers,
        adapter_factory=_EchoAdapter,
        metrics_store=store,
    )
    threads, invokes = 12, 5
    served: list[str] = []

    def worker() -> None:
        for _ in range(invokes):
            served.append(router.invoke(_calls()))

    errors = _run_threads(worker, threads)

    assert errors == []
    assert len(served) == threads * invokes
    events = store.query_recent(since=_SINCE)
    assert len(events) == threads * invokes
    assert all(event.success for event in events)
    report = router.health_report()
    assert all(
        entry.consecutive_failures == 0 and entry.cooldown_remaining_seconds is None
        for entry in report.values()
    )
    store.close()


def test_round_robin_stays_an_exact_rotation_under_concurrency() -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    router = ProviderRouter(
        metrics_scope="test",
        providers=providers,
        adapter_factory=_EchoAdapter,
        metrics_store=None,
    )
    threads, invokes = 6, 10
    served: list[str] = []

    def worker() -> None:
        for _ in range(invokes):
            served.append(router.invoke(_calls()))

    errors = _run_threads(worker, threads)

    assert errors == []
    # 60 invokes over 3 providers: a lock-protected rotation serves each
    # exactly 20 times, while a lost-update race on the index skews the split.
    expected = threads * invokes // len(providers)
    assert Counter(served) == {provider.provider_id: expected for provider in providers}


def test_concurrent_first_calls_build_one_default_adapter() -> None:
    provider = _config("provider_a")
    router = ProviderRouter(metrics_scope="test", providers=[provider], metrics_store=None)
    adapters: list[object] = []

    errors = _run_threads(lambda: adapters.append(router._adapter_for(provider)), 8)

    assert errors == []
    # Identity is the honest observable (matching test_http_client_reuse):
    # every thread must receive the one cached adapter, not a duplicate that
    # would carry its own SDK client and connection pool.
    assert len({id(adapter) for adapter in adapters}) == 1
    assert set(router._default_adapter_cache) == {"provider_a"}
