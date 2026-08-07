from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_package_root_import_succeeds_when_optional_sdks_are_unavailable() -> None:
    """Prove the core import does not merely benefit from SDKs installed in the dev venv."""
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import importlib.abc
import sys

class OptionalDependencyBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in {{
            "duckdb",
            "httpx",
            "langchain",
            "langchain_core",
            "openai",
            "opentelemetry",
            "prometheus_client",
            "psycopg",
            "sqlalchemy",
            "supabase",
        }}:
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, OptionalDependencyBlocker())
sys.path.insert(0, {str(source_root)!r})
from llm_provider_router import (
    ErrorCategory,
    LocalBackend,
    PostgresConfig,
    PostgresMetricsStore,
    PostgresPoolMode,
    ProviderRouter,
    RetryContext,
    RetryPolicy,
    RetryProviderScope,
    SameProviderRetryPolicy,
    inspect_database,
    redact_postgres_url,
)
assert ProviderRouter.__name__ == "ProviderRouter"
assert RetryPolicy.__name__ == "RetryPolicy"
assert RetryContext.__name__ == "RetryContext"
assert RetryProviderScope.FIRST.value == "first"
assert SameProviderRetryPolicy().max_attempts == 3
assert ErrorCategory.TIMEOUT.value == "timeout"
assert LocalBackend.SQLITE.value == "sqlite"
assert callable(inspect_database)

# Exporting the PostgreSQL store must not make psycopg mandatory, and
# constructing one without the driver reports unavailability rather than
# raising -- the failure belongs at first use, with an install hint.
assert PostgresPoolMode.DIRECT.value == "direct"
assert PostgresConfig().statement_timeout_seconds == 2.0
assert redact_postgres_url("postgresql://u:pw@h/db") == "postgresql://u:***@h/db"
store = PostgresMetricsStore("postgresql://u:pw@h/db")
assert store.available is False
assert store.effective_sslmode == "require"
try:
    store.query_recent(since=__import__("datetime").datetime.now(__import__("datetime").UTC))
except ImportError as exc:
    assert "llm-provider-router[postgres]" in str(exc)
else:
    raise AssertionError("expected an ImportError naming the postgres extra")
assert not ({{
    "duckdb",
    "httpx",
    "langchain",
    "langchain_core",
    "openai",
    "opentelemetry",
    "prometheus_client",
    "psycopg",
    "sqlalchemy",
    "supabase",
}} & sys.modules.keys())
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
