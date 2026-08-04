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
        }}:
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, OptionalDependencyBlocker())
sys.path.insert(0, {str(source_root)!r})
from nygen_router import (
    ErrorCategory,
    ProviderRouter,
    RetryContext,
    RetryPolicy,
    RetryProviderScope,
    SameProviderRetryPolicy,
)
assert ProviderRouter.__name__ == "ProviderRouter"
assert RetryPolicy.__name__ == "RetryPolicy"
assert RetryContext.__name__ == "RetryContext"
assert RetryProviderScope.FIRST.value == "first"
assert SameProviderRetryPolicy().max_attempts == 3
assert ErrorCategory.TIMEOUT.value == "timeout"
assert not ({{
    "duckdb",
    "httpx",
    "langchain",
    "langchain_core",
    "openai",
    "opentelemetry",
    "prometheus_client",
}} & sys.modules.keys())
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
