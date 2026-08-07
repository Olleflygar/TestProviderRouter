from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from llm_provider_router.cli import main


def _run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "llm_provider_router.cli", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_help_loads_without_selecting_a_backend() -> None:
    result = _run("--help")

    assert result.returncode == 0
    assert "storage" in result.stdout
    assert result.stderr == ""


def test_cli_main_directly_formats_inspect_create_migrate_and_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "direct.sqlite"
    backup = tmp_path / "direct-backup.sqlite"

    assert main(["storage", "inspect", "--backend", "sqlite", "--path", str(path)]) == 0
    assert "Schema state: missing" in capsys.readouterr().out
    assert main(["storage", "create", "--backend", "sqlite", "--path", str(path)]) == 0
    assert "Latest metrics version: 2" in capsys.readouterr().out
    assert (
        main(
            [
                "storage",
                "migrate",
                "--backend",
                "sqlite",
                "--path",
                str(path),
                "--backup",
                str(backup),
            ]
        )
        == 0
    )
    assert "Final validation: current" in capsys.readouterr().out

    assert main(["storage", "create", "--backend", "sqlite", "--path", str(path)]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "left untouched" in captured.err


def test_cli_main_directly_reports_busy_and_general_admin_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "busy.sqlite"
    assert main(["storage", "create", "--backend", "sqlite", "--path", str(path)]) == 0
    capsys.readouterr()
    blocker = sqlite3.connect(str(path))
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert main(["storage", "migrate", "--backend", "sqlite", "--path", str(path)]) == 5
    finally:
        blocker.rollback()
        blocker.close()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Stop the application" in captured.err
    assert "source was not partially migrated" in captured.err

    parent_file = tmp_path / "occupied"
    parent_file.write_text("keep")
    impossible = parent_file / "metrics.sqlite"
    assert main(["storage", "create", "--backend", "sqlite", "--path", str(impossible)]) == 6
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "operation-owned partial target" in captured.err
    assert "inspect the unchanged or rolled-back source" in captured.err


def test_sqlite_inspect_and_create_output_is_actionable(tmp_path: Path) -> None:
    path = tmp_path / "chosen" / "metrics.sqlite"

    inspected = _run("storage", "inspect", "--backend", "sqlite", "--path", str(path))
    created = _run("storage", "create", "--backend", "sqlite", "--path", str(path))

    assert inspected.returncode == 0
    assert "Schema state: missing" in inspected.stdout
    assert "Next safe action:" in inspected.stdout
    assert not inspected.stderr
    assert created.returncode == 0
    assert f"Created sqlite database: {path.resolve()}" in created.stdout
    assert "Latest metrics version: 2" in created.stdout
    assert "ProviderRouter will not discover" in created.stdout
    assert f"SQLiteMetricsStore({str(path.resolve())!r})" in created.stdout
    assert created.stderr == ""


def test_default_duckdb_create_says_no_explicit_path_is_needed(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}

    result = _run("storage", "create", "--backend", "duckdb", "--default", env=env)

    assert result.returncode == 0
    assert str(tmp_path / ".nygen_router" / "metrics.duckdb") in result.stdout
    assert "Router default path: yes" in result.stdout
    assert "no explicit path is needed" in result.stdout
    assert "will not discover" not in result.stdout


def test_create_existing_target_uses_stderr_and_stable_nonzero_status(tmp_path: Path) -> None:
    path = tmp_path / "metrics.sqlite"
    first = _run("storage", "create", "--backend", "sqlite", "--path", str(path))
    second = _run("storage", "create", "--backend", "sqlite", "--path", str(path))

    assert first.returncode == 0
    assert second.returncode == 4
    assert second.stdout == ""
    assert "Error:" in second.stderr
    assert "left untouched" in second.stderr


def test_cli_migrate_reports_steps_backup_and_final_validation(tmp_path: Path) -> None:
    path = tmp_path / "metrics.sqlite"
    backup = tmp_path / "backup.sqlite"
    _run("storage", "create", "--backend", "sqlite", "--path", str(path))

    result = _run(
        "storage",
        "migrate",
        "--backend",
        "sqlite",
        "--path",
        str(path),
        "--backup",
        str(backup),
    )

    assert result.returncode == 0
    assert "Source metrics version: 2" in result.stdout
    assert "Target metrics version: 2" in result.stdout
    assert "Planned steps: <none>" in result.stdout
    assert "Completed steps: <none>" in result.stdout
    assert "No-op: yes" in result.stdout
    assert f"Backup: {backup.resolve()}" in result.stdout
    assert "Final validation: current" in result.stdout


def test_cli_rejects_sqlite_default_as_invalid_arguments() -> None:
    result = _run("storage", "inspect", "--backend", "sqlite", "--default")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--default is available only" in result.stderr


def test_cli_sqlite_admin_works_when_duckdb_import_is_blocked(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1] / "src"
    path = tmp_path / "stdlib.sqlite"
    code = f"""
import importlib.abc
import sys

class DuckDBBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition('.')[0] == 'duckdb':
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, DuckDBBlocker())
sys.path.insert(0, {str(source_root)!r})
from llm_provider_router.cli import main
raise SystemExit(main(['storage', 'create', '--backend', 'sqlite', '--path', {str(path)!r}]))
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert path.exists()
    assert "Latest metrics version: 2" in result.stdout
    assert "duckdb" not in result.stderr.lower()


def test_cli_duckdb_admin_has_concise_dependency_error_when_import_is_blocked(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).parents[1] / "src"
    path = tmp_path / "missing.duckdb"
    code = f"""
import importlib.abc
import sys

class DuckDBBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition('.')[0] == 'duckdb':
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, DuckDBBlocker())
sys.path.insert(0, {str(source_root)!r})
from llm_provider_router.cli import main
raise SystemExit(main(['storage', 'inspect', '--backend', 'duckdb', '--path', {str(path)!r}]))
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 3
    assert result.stdout == ""
    assert "pip install" in result.stderr
    assert "llm-provider-router[duckdb]" in result.stderr
    assert "Traceback" not in result.stderr
