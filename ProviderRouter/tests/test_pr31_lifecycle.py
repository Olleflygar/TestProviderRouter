"""PR31 lifecycle: close() is idempotent, terminal, and owns only what it created.

The connection-identity assertions read store internals directly, following
test_http_client_reuse's precedent: with lazy reconnect there is no behavioral
difference between a closed and an open store, so the connection attribute is
the only honest observable for "released and never resurrected".
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    NormalizedStream,
    ProviderConfig,
    ProviderRouter,
    RouterClosedError,
    SQLiteMetricsStore,
)

_DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None
requires_duckdb = pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed")

_SINCE = datetime(2000, 1, 1, tzinfo=UTC)


def _config(name: str = "provider_a") -> ProviderConfig:
    return ProviderConfig(
        provider_id=name,
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
    )


def _calls(call_type: CallType = CallType.REGULAR) -> list[CallVariant]:
    return [
        CallVariant(
            call_type=call_type,
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


class _FakeStream(NormalizedStream):
    """A scripted stream whose last chunk carries the completion marker."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self._completed = False

    def __next__(self) -> Any:
        if self._index >= len(self._chunks):
            raise StopIteration
        chunk = self._chunks[self._index]
        if self._index == len(self._chunks) - 1:
            self._completed = True
        self._index += 1
        return chunk

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def recognized(self) -> bool:
        return True

    @property
    def usage(self) -> Any:
        return None

    def close(self) -> None:
        pass


class _StreamAdapter:
    """Returns one fresh two-chunk completing stream per invocation."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    def invoke(self, operation: str, arguments: dict[str, object]) -> _FakeStream:
        return _FakeStream(["chunk-1", "chunk-2"])


def test_close_is_idempotent_and_terminal() -> None:
    router = ProviderRouter(
        metrics_scope="test",
        providers=[_config()],
        adapter_factory=_EchoAdapter,
        metrics_store=None,
    )
    assert router.invoke(_calls()) == "provider_a"

    router.close()
    router.close()  # must not raise

    with pytest.raises(RouterClosedError):
        router.invoke(_calls())


def test_context_manager_closes_on_exit() -> None:
    with ProviderRouter(
        metrics_scope="test",
        providers=[_config()],
        adapter_factory=_EchoAdapter,
        metrics_store=None,
    ) as router:
        assert router.invoke(_calls()) == "provider_a"

    with pytest.raises(RouterClosedError):
        router.invoke(_calls())


def test_close_before_first_use_is_safe() -> None:
    router = ProviderRouter(
        metrics_scope="test",
        providers=[_config()],
        adapter_factory=_EchoAdapter,
    )

    router.close()

    assert router._owned_metrics_store is not None
    assert router._owned_metrics_store._connection is None
    with pytest.raises(RouterClosedError):
        router.invoke(_calls())


@requires_duckdb
def test_close_closes_the_router_created_default_store() -> None:
    router = ProviderRouter(
        metrics_scope="test",
        providers=[_config()],
        adapter_factory=_EchoAdapter,
    )
    router.invoke(_calls())  # first recorded attempt opens the default store
    owned = router._owned_metrics_store
    assert owned is not None
    assert owned._connection is not None

    router.close()

    assert owned._connection is None


def test_close_never_touches_a_caller_provided_store(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite")
    router = ProviderRouter(
        metrics_scope="test",
        providers=[_config()],
        adapter_factory=_EchoAdapter,
        metrics_store=store,
    )
    router.invoke(_calls())
    assert store._connection is not None

    router.close()

    # Close-what-you-create: the caller's store stays open and fully usable.
    assert store._connection is not None
    assert len(store.query_recent(since=_SINCE)) == 1
    store.close()


def test_stream_finishing_after_close_drops_only_its_metrics_row(tmp_path: Path) -> None:
    store = SQLiteMetricsStore(tmp_path / "metrics.sqlite")
    router = ProviderRouter(
        metrics_scope="test",
        providers=[_config()],
        adapter_factory=_StreamAdapter,
        metrics_store=store,
    )
    stream = router.invoke(_calls(CallType.STREAMING))
    first = next(stream)

    router.close()
    rest = list(stream)  # the consumer keeps draining past close, untouched

    assert [first, *rest] == ["chunk-1", "chunk-2"]
    # The stream served its content; only its bookkeeping row is dropped,
    # visibly counted, instead of being written to a router already closed.
    assert len(store.query_recent(since=_SINCE)) == 0
    assert router._dropped_metrics_events == 1
    store.close()


@requires_duckdb
def test_late_stream_does_not_reopen_the_closed_default_store() -> None:
    router = ProviderRouter(
        metrics_scope="test",
        providers=[_config()],
        adapter_factory=_StreamAdapter,
    )
    stream = router.invoke(_calls(CallType.STREAMING))
    next(stream)

    router.close()
    remaining = list(stream)

    assert remaining == ["chunk-2"]
    owned = router._owned_metrics_store
    assert owned is not None
    # The late bookkeeping write must not resurrect the connection close()
    # released -- that would silently re-take DuckDB's exclusive file lock.
    assert owned._connection is None
    assert router._dropped_metrics_events == 1


def test_health_surface_keeps_working_after_close() -> None:
    router = ProviderRouter(
        metrics_scope="test",
        providers=[_config()],
        adapter_factory=_EchoAdapter,
        metrics_store=None,
    )
    router.invoke(_calls())

    router.close()

    assert set(router.health_report()) == {"provider_a"}
    router.reset_health()  # in-memory state stays inspectable and resettable
