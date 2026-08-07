from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _close_shared_postgres_store() -> Iterator[None]:
    """Release the PostgreSQL test pool before the interpreter tears down.

    Closing it at exit instead races with interpreter shutdown and prints a
    "couldn't stop thread" warning. No-op when no test database is configured.
    """
    yield
    from postgres_helpers import forget_shared_store

    forget_shared_store()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect HOME so ProviderRouter's default DuckDBMetricsStore never
    touches the real home directory.

    Now that ProviderRouter() with no metrics_store constructs a real
    DuckDBMetricsStore() pointed at ~/.nygen_router/metrics.duckdb, every
    existing test that doesn't pass metrics_store=None would otherwise write
    to the developer's actual home directory on every invoke().
    """
    monkeypatch.setenv("HOME", str(tmp_path))
