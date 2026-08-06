from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nygen_router import ApiProtocol, CallType, MetricsEvent
from nygen_router.storage.admin import (
    LocalBackend,
    StorageAdministrationError,
    StorageBackupError,
    StorageBusyError,
    StorageCompatibilityError,
    StorageTargetError,
    create_database,
    inspect_database,
    migrate_database,
)
from nygen_router.storage.base import event_to_params
from nygen_router.storage.schema import (
    CREATE_PROVIDER_ATTEMPTS_TABLE_SQL,
    CREATE_SCHEMA_VERSIONS_TABLE_SQL,
    METRICS_COMPONENT,
    SCHEMA_VERSIONS_TABLE,
    MetadataState,
    SchemaState,
)

_DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None


@pytest.fixture(
    params=[
        pytest.param((LocalBackend.SQLITE, ".sqlite"), id="sqlite"),
        pytest.param(
            (LocalBackend.DUCKDB, ".duckdb"),
            id="duckdb",
            marks=pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed"),
        ),
    ]
)
def target(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[LocalBackend, Path]:
    backend, suffix = request.param
    return backend, tmp_path / "nested" / f"metrics{suffix}"


def _event() -> MetricsEvent:
    return MetricsEvent(
        id="preserved-event",
        timestamp=datetime.now(UTC),
        metrics_scope="scope-a",
        provider_id="provider-a",
        provider_name="Provider A",
        model="model-a",
        protocol=ApiProtocol.OPENAI_CHAT,
        call_type=CallType.REGULAR,
        success=True,
        latency_ms=4.5,
    )


def _connect(backend: LocalBackend, path: Path) -> object:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backend is LocalBackend.SQLITE:
        return sqlite3.connect(str(path))
    import duckdb

    return duckdb.connect(str(path))


def _execute(backend: LocalBackend, path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = _connect(backend, path)
    try:
        connection.execute(sql, params)  # type: ignore[attr-defined]
        if backend is LocalBackend.SQLITE:
            connection.commit()  # type: ignore[attr-defined]
    finally:
        connection.close()  # type: ignore[attr-defined]


def _create_implicit(backend: LocalBackend, path: Path) -> MetricsEvent:
    event = _event()
    _execute(backend, path, CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
    params = event_to_params(event)
    _execute(
        backend,
        path,
        f"INSERT INTO provider_attempts VALUES ({', '.join('?' for _ in params)})",
        params,
    )
    return event


def _read_event_ids(backend: LocalBackend, path: Path) -> list[str]:
    connection = _connect(backend, path)
    try:
        rows = connection.execute(  # type: ignore[attr-defined]
            "SELECT id FROM provider_attempts ORDER BY timestamp"
        ).fetchall()
    finally:
        connection.close()  # type: ignore[attr-defined]
    return [str(row[0]) for row in rows]


def test_inspect_absent_target_creates_neither_parent_nor_file(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target

    result = inspect_database(backend, path)

    assert result.exists is False
    assert result.schema.state is SchemaState.MISSING
    assert not path.exists()
    assert not path.parent.exists()


def test_python_api_requires_typed_backend_and_explicit_sqlite_path(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="LocalBackend"):
        inspect_database("sqlite", tmp_path / "metrics.sqlite")  # type: ignore[arg-type]
    with pytest.raises(StorageTargetError, match="explicit database path"):
        inspect_database(LocalBackend.SQLITE)


def test_migrate_requires_an_existing_target(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"

    with pytest.raises(StorageTargetError, match="Cannot migrate missing"):
        migrate_database(LocalBackend.SQLITE, path)

    assert not path.exists()


def test_inspect_reports_arbitrary_existing_target_as_unknown(tmp_path: Path) -> None:
    path = tmp_path / "arbitrary.sqlite"
    path.write_bytes(b"not a sqlite database")

    inspection = inspect_database(LocalBackend.SQLITE, path)

    assert inspection.schema.state is SchemaState.UNKNOWN
    assert inspection.schema.metadata_state is MetadataState.INVALID
    assert path.read_bytes() == b"not a sqlite database"


def test_create_failure_does_not_remove_preexisting_parent_file(tmp_path: Path) -> None:
    parent_file = tmp_path / "occupied"
    parent_file.write_text("keep parent")
    target = parent_file / "metrics.sqlite"

    with pytest.raises(StorageAdministrationError, match="partial target"):
        create_database(LocalBackend.SQLITE, target)

    assert parent_file.read_text() == "keep parent"


def test_create_builds_current_schema_and_refuses_a_second_create(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    result = create_database(backend, path)
    before = path.read_bytes()

    assert result.path == path.resolve()
    assert result.validation.schema.state is SchemaState.CURRENT
    assert result.validation.schema.metrics_version == 1
    with pytest.raises(StorageTargetError, match="already exists"):
        create_database(backend, path)
    assert path.read_bytes() == before


def test_create_refuses_arbitrary_existing_file_byte_for_byte(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database and must remain exactly intact")

    with pytest.raises(StorageTargetError, match="left untouched"):
        create_database(backend, path)

    assert path.read_bytes() == b"not a database and must remain exactly intact"


def test_inspect_is_read_only_for_current_schema_and_rows(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    create_database(backend, path)
    before = path.read_bytes()

    first = inspect_database(backend, path)
    second = inspect_database(backend, path)

    assert first == second
    assert first.schema.state is SchemaState.CURRENT
    assert path.read_bytes() == before


def test_migrate_stamps_exact_implicit_baseline_preserves_events_and_is_idempotent(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    event = _create_implicit(backend, path)

    migrated = migrate_database(backend, path)
    repeated = migrate_database(backend, path)

    assert migrated.source_version is None
    assert [step.name for step in migrated.planned_steps] == [
        "stamp exact implicit metrics baseline as version 1"
    ]
    assert migrated.completed_steps == migrated.planned_steps
    assert migrated.no_op is False
    assert repeated.no_op is True
    assert _read_event_ids(backend, path) == [event.id]
    assert inspect_database(backend, path).schema.state is SchemaState.CURRENT


def test_optional_backup_is_validated_and_remains_pre_migration_source(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    event = _create_implicit(backend, path)
    backup = path.with_name(f"backup{path.suffix}")

    result = migrate_database(backend, path, backup_path=backup)

    assert result.backup_path == backup.resolve()
    assert inspect_database(backend, backup).schema.state is SchemaState.IMPLICIT_BASELINE
    assert inspect_database(backend, path).schema.state is SchemaState.CURRENT
    assert _read_event_ids(backend, backup) == [event.id]


def test_backup_refuses_existing_destination_before_source_changes(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    _create_implicit(backend, path)
    backup = path.with_name(f"backup{path.suffix}")
    backup.write_bytes(b"keep this backup target")

    with pytest.raises(StorageBackupError, match="already exists"):
        migrate_database(backend, path, backup_path=backup)

    assert backup.read_bytes() == b"keep this backup target"
    assert inspect_database(backend, path).schema.state is SchemaState.IMPLICIT_BASELINE


def test_missing_metrics_component_is_rejected_without_modification(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    _execute(backend, path, CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
    _execute(backend, path, CREATE_SCHEMA_VERSIONS_TABLE_SQL)
    _execute(
        backend,
        path,
        f"INSERT INTO {SCHEMA_VERSIONS_TABLE} VALUES (?, ?)",
        ("health", 1),
    )
    before = path.read_bytes()

    inspection = inspect_database(backend, path)
    with pytest.raises(StorageCompatibilityError, match="no required metrics"):
        migrate_database(backend, path)

    assert inspection.schema.state is SchemaState.UNKNOWN
    assert inspection.schema.metadata_state is MetadataState.VALID
    assert path.read_bytes() == before


def test_newer_version_is_rejected_without_modification(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    create_database(backend, path)
    _execute(
        backend,
        path,
        f"UPDATE {SCHEMA_VERSIONS_TABLE} SET version = 2 WHERE component = ?",
        (METRICS_COMPONENT,),
    )
    before = path.read_bytes()

    with pytest.raises(StorageCompatibilityError, match="newer than installed"):
        migrate_database(backend, path)

    assert path.read_bytes() == before


def test_valid_unrelated_component_survives_current_no_op_migration(
    target: tuple[LocalBackend, Path],
) -> None:
    backend, path = target
    create_database(backend, path)
    _execute(
        backend,
        path,
        f"INSERT INTO {SCHEMA_VERSIONS_TABLE} VALUES (?, ?)",
        ("health", 9),
    )

    result = migrate_database(backend, path)

    assert result.no_op is True
    assert [(item.component, item.version) for item in result.validation.schema.components] == [
        ("health", 9),
        ("metrics", 1),
    ]


def test_malformed_duplicate_component_metadata_is_reported_read_only(tmp_path: Path) -> None:
    path = tmp_path / "duplicates.sqlite"
    _execute(LocalBackend.SQLITE, path, CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
    _execute(
        LocalBackend.SQLITE,
        path,
        f"CREATE TABLE {SCHEMA_VERSIONS_TABLE} (component TEXT, version INTEGER NOT NULL)",
    )
    _execute(
        LocalBackend.SQLITE,
        path,
        f"INSERT INTO {SCHEMA_VERSIONS_TABLE} VALUES ('metrics', 1), ('metrics', 1)",
    )
    before = path.read_bytes()

    inspection = inspect_database(LocalBackend.SQLITE, path)

    assert inspection.schema.state is SchemaState.UNKNOWN
    assert inspection.schema.metadata_state is MetadataState.INVALID
    assert "duplicate component" in inspection.schema.detail
    assert [(item.component, item.version) for item in inspection.schema.components] == [
        ("metrics", 1)
    ]
    assert path.read_bytes() == before


@pytest.mark.parametrize("invalid_version", [0, -1, "not-an-integer"])
def test_nonpositive_and_noninteger_versions_are_rejected_read_only(
    tmp_path: Path, invalid_version: object
) -> None:
    path = tmp_path / f"invalid-{invalid_version}.sqlite"
    _execute(LocalBackend.SQLITE, path, CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
    _execute(LocalBackend.SQLITE, path, CREATE_SCHEMA_VERSIONS_TABLE_SQL)
    _execute(
        LocalBackend.SQLITE,
        path,
        f"INSERT INTO {SCHEMA_VERSIONS_TABLE} VALUES (?, ?)",
        (METRICS_COMPONENT, invalid_version),
    )
    before = path.read_bytes()

    inspection = inspect_database(LocalBackend.SQLITE, path)
    with pytest.raises(StorageCompatibilityError):
        migrate_database(LocalBackend.SQLITE, path)

    assert inspection.schema.state is SchemaState.UNKNOWN
    assert inspection.schema.metadata_state is MetadataState.INVALID
    assert "positive integer" in inspection.schema.detail
    assert path.read_bytes() == before


def test_sqlite_busy_database_fails_offline_migration_without_partial_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "busy.sqlite"
    _create_implicit(LocalBackend.SQLITE, path)
    blocker = sqlite3.connect(str(path))
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(StorageBusyError, match="busy"):
            migrate_database(LocalBackend.SQLITE, path)
    finally:
        blocker.rollback()
        blocker.close()

    assert inspect_database(LocalBackend.SQLITE, path).schema.state is SchemaState.IMPLICIT_BASELINE


@pytest.mark.skipif(not _DUCKDB_AVAILABLE, reason="duckdb is not installed")
def test_duckdb_busy_database_fails_offline_migration_without_partial_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "busy.duckdb"
    _create_implicit(LocalBackend.DUCKDB, path)
    code = (
        "import duckdb, sys; "
        f"connection = duckdb.connect({str(path)!r}); "
        "print('ready', flush=True); sys.stdin.readline(); connection.close()"
    )
    blocker = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert blocker.stdout is not None
    assert blocker.stdout.readline().strip() == "ready"
    try:
        with pytest.raises(StorageBusyError, match="busy"):
            migrate_database(LocalBackend.DUCKDB, path)
    finally:
        assert blocker.stdin is not None
        blocker.stdin.write("stop\n")
        blocker.stdin.flush()
        blocker.wait(timeout=10)

    assert inspect_database(LocalBackend.DUCKDB, path).schema.state is SchemaState.IMPLICIT_BASELINE
