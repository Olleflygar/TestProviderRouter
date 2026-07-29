from __future__ import annotations

from pathlib import Path

import pytest


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
