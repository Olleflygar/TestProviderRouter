from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nygen_router.storage.schema import (
    CREATE_DUCKDB_REQUIRED_METRICS_INDEX_SQL,
    CREATE_POSTGRES_REQUIRED_METRICS_INDEX_SQL,
    CREATE_PROVIDER_ATTEMPTS_TABLE_POSTGRES_SQL,
    CREATE_PROVIDER_ATTEMPTS_TABLE_SQL,
    CREATE_SCHEMA_VERSIONS_TABLE_POSTGRES_SQL,
    CREATE_SCHEMA_VERSIONS_TABLE_SQL,
    CREATE_SQLITE_REQUIRED_METRICS_INDEX_SQL,
    INSERT_METRICS_VERSION_SQL,
    METRICS_COMPONENT,
    METRICS_SCHEMA_VERSION,
    SCHEMA_VERSIONS_TABLE,
    SELECT_POSTGRES_TABLES_SQL,
    MetadataState,
    SchemaReport,
    SchemaState,
    missing_schema_report,
    validate_created_schema,
)


class LocalBackend(StrEnum):
    """Local database engines supported by the administration surface."""

    DUCKDB = "duckdb"
    SQLITE = "sqlite"


@dataclass(frozen=True)
class DatabaseInspection:
    backend: LocalBackend
    path: Path
    exists: bool
    is_default_path: bool
    schema: SchemaReport


@dataclass(frozen=True)
class DatabaseCreation:
    backend: LocalBackend
    path: Path
    is_default_path: bool
    metrics_version: int
    validation: DatabaseInspection


@dataclass(frozen=True)
class MigrationStep:
    source_version: int | None
    target_version: int
    name: str


@dataclass(frozen=True)
class DatabaseMigration:
    backend: LocalBackend
    path: Path
    source_version: int | None
    target_version: int
    planned_steps: tuple[MigrationStep, ...]
    completed_steps: tuple[MigrationStep, ...]
    no_op: bool
    backup_path: Path | None
    validation: DatabaseInspection


class StorageAdministrationError(RuntimeError):
    """Base class for safe, actionable local-storage administration failures."""


class StorageDependencyError(StorageAdministrationError):
    """An explicitly selected optional backend is not installed."""


class StorageTargetError(StorageAdministrationError):
    """The selected filesystem target cannot be used for this operation."""


class StorageCompatibilityError(StorageAdministrationError):
    """A target has no complete, known migration route."""


class StorageBusyError(StorageAdministrationError):
    """The offline migration lock could not be acquired."""


class StorageBackupError(StorageAdministrationError):
    """An explicitly requested pre-migration backup could not be validated."""


# PR30 deliberately ships no version-1/implicit-v1 migration. Future schema
# changes append real consecutive, executable steps here.
_MIGRATION_REGISTRY: tuple[MigrationStep, ...] = ()


def default_duckdb_path() -> Path:
    return (Path.home() / ".nygen_router" / "metrics.duckdb").resolve()


def inspect_database(backend: LocalBackend, path: str | Path | None = None) -> DatabaseInspection:
    """Inspect one deliberately selected local target without writing anything."""
    if not isinstance(backend, LocalBackend):
        raise TypeError("backend must be a LocalBackend value")
    resolved, is_default = _resolve_target(backend, path)
    if backend is LocalBackend.DUCKDB:
        _require_duckdb()
    if not resolved.exists():
        return DatabaseInspection(
            backend=backend,
            path=resolved,
            exists=False,
            is_default_path=is_default,
            schema=missing_schema_report(),
        )
    try:
        report = (
            _inspect_sqlite_path(resolved)
            if backend is LocalBackend.SQLITE
            else _inspect_duckdb_path(resolved)
        )
    except Exception as exc:
        if isinstance(exc, StorageAdministrationError):
            raise
        if _looks_busy(exc):
            raise StorageBusyError(
                f"Could not inspect {backend.value} database at {str(resolved)!r} because it is "
                "busy. Stop the application, routers, and other writers, then retry."
            ) from exc
        report = SchemaReport(
            state=SchemaState.UNKNOWN,
            metadata_state=MetadataState.INVALID,
            components=(),
            metrics_version=None,
            compatible=False,
            next_action=(
                "Preserve the target, stop all writers, and verify that it is a readable database "
                "for the selected backend; otherwise choose and configure a different absent path."
            ),
            detail="The existing target could not be read as the selected database backend.",
        )
    return DatabaseInspection(
        backend=backend,
        path=resolved,
        exists=True,
        is_default_path=is_default,
        schema=report,
    )


def create_database(backend: LocalBackend, path: str | Path | None = None) -> DatabaseCreation:
    """Create and validate a brand-new metrics-v2 database, refusing existing targets."""
    before = inspect_database(backend, path)
    if before.exists:
        raise StorageTargetError(
            f"Refusing to create {backend.value} database at {str(before.path)!r}: the target "
            "already exists and was left untouched. Inspect it, migrate it when supported, "
            "manually move/archive it while the application is stopped, or choose a different "
            "path and configure that concrete store."
        )
    temporary: Path | None = None
    published = False
    try:
        before.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_database_path(before.path)
        if backend is LocalBackend.SQLITE:
            _create_sqlite(temporary)
        else:
            _create_duckdb(temporary)
        temporary_validation = inspect_database(backend, temporary)
        validate_created_schema(temporary_validation.schema)
        try:
            os.link(temporary, before.path)
        except FileExistsError as exc:
            raise StorageTargetError(
                f"Refusing to create {backend.value} database at {str(before.path)!r}: the "
                "target appeared concurrently and was left untouched. Inspect it or choose a "
                "different absent path."
            ) from exc
        published = True
        temporary.unlink()
        validation = inspect_database(backend, before.path)
        validate_created_schema(validation.schema)
    except BaseException as exc:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        if published and before.path.exists():
            before.path.unlink()
        if isinstance(exc, StorageAdministrationError):
            raise
        raise StorageAdministrationError(
            f"Could not create {backend.value} database at {str(before.path)!r}; the "
            "operation-owned partial target was removed when safe."
        ) from exc
    return DatabaseCreation(
        backend=backend,
        path=before.path,
        is_default_path=before.is_default_path,
        metrics_version=METRICS_SCHEMA_VERSION,
        validation=validation,
    )


def migrate_database(
    backend: LocalBackend,
    path: str | Path | None = None,
    *,
    backup_path: str | Path | None = None,
) -> DatabaseMigration:
    """Run a registered offline route; PR30 provides no v1/implicit-v1 route."""
    before = inspect_database(backend, path)
    if not before.exists:
        raise StorageTargetError(
            f"Cannot migrate missing {backend.value} database at {str(before.path)!r}; create it "
            "instead. Migration is only for an existing supported database."
        )
    planned = _build_route(before)
    resolved_backup = None if backup_path is None else _resolve_backup_path(backup_path)
    source_signature = _database_signature(backend, before.path, before)
    if resolved_backup is not None:
        _create_validated_backup(
            backend,
            source=before.path,
            destination=resolved_backup,
            expected_signature=source_signature,
        )

    try:
        completed = (
            _migrate_sqlite(before.path, planned, source_signature)
            if backend is LocalBackend.SQLITE
            else _migrate_duckdb(before.path, planned, source_signature)
        )
    except Exception as exc:
        if isinstance(exc, StorageAdministrationError):
            raise
        if _looks_busy(exc):
            raise StorageBusyError(
                f"Could not acquire the exclusive offline migration lock for {backend.value} "
                f"database at {str(before.path)!r}. Stop the application, all routers, and other "
                "database writers, then retry; no migration step was committed."
            ) from exc
        raise StorageAdministrationError(
            f"Migration failed for {backend.value} database at {str(before.path)!r}. The "
            "transaction was rolled back; keep any requested backup and inspect the source before "
            "retrying."
        ) from exc

    validation = inspect_database(backend, before.path)
    if validation.schema.state is not SchemaState.CURRENT:
        raise StorageAdministrationError(
            f"Migration transaction completed but final validation failed for {backend.value} "
            f"database at {str(before.path)!r}; inspect the source and retain any backup."
        )
    return DatabaseMigration(
        backend=backend,
        path=before.path,
        source_version=(
            None
            if before.schema.state is SchemaState.IMPLICIT_BASELINE
            else before.schema.metrics_version
        ),
        target_version=METRICS_SCHEMA_VERSION,
        planned_steps=planned,
        completed_steps=completed,
        no_op=not planned,
        backup_path=resolved_backup,
        validation=validation,
    )


# Administration is a deliberate, interactive act with no hot path to protect,
# so it waits rather than failing fast. The store's shipped defaults are tuned
# for the opposite case -- bookkeeping that must never delay a provider
# response -- and would report a distant but healthy database as unreachable.
_ADMIN_CONNECTION_CONFIG = {
    "connect_timeout_seconds": 15.0,
    "statement_timeout_seconds": 30.0,
    "checkout_timeout_seconds": 15.0,
}


@dataclass(frozen=True)
class PostgresInspection:
    """Read-only diagnosis of one deliberately selected PostgreSQL target."""

    target: str
    exists: bool
    schema: SchemaReport


@dataclass(frozen=True)
class PostgresCreation:
    target: str
    metrics_version: int
    validation: PostgresInspection


def _admin_connect(url: str) -> Any:
    """Open one short-lived administrative connection.

    Administration reads the catalog once and stops, so it uses a plain
    connection rather than building a pool: a pool per command is pure churn
    against a server's bounded connection budget.
    """
    psycopg = _require_psycopg()
    return psycopg.connect(
        url,
        connect_timeout=int(_ADMIN_CONNECTION_CONFIG["connect_timeout_seconds"]),
    )


def inspect_postgres_database(url: str) -> PostgresInspection:
    """Inspect a PostgreSQL metrics schema without writing anything."""
    from nygen_router.storage.postgres import _inspect_connection, redact_postgres_url

    target = redact_postgres_url(url)
    try:
        with _admin_connect(url) as connection:
            report = _inspect_connection(connection)
    except Exception as exc:
        if isinstance(exc, StorageAdministrationError):
            raise
        raise StorageTargetError(
            f"Could not inspect PostgreSQL metrics database at {target!r}. The existing target "
            "was left untouched; verify the connection details, the role's read access, and the "
            "search_path."
        ) from exc
    return PostgresInspection(
        target=target,
        exists=report.state is not SchemaState.MISSING,
        schema=report,
    )


def create_postgres_database(url: str) -> PostgresCreation:
    """Create the metrics schema in one transaction, refusing an occupied target.

    This is a deliberate administrative act run with a schema-owning role. The
    router never reaches this code: it neither creates nor alters a remote
    schema at construction or during a call.
    """
    from nygen_router.storage.postgres import _inspect_connection, redact_postgres_url

    target = redact_postgres_url(url)
    try:
        # Creating and validating share one connection: a second one would add
        # another failure point and another round of connection setup for a
        # check the same session can make immediately after committing.
        with _admin_connect(url) as connection:
            existing = {
                str(row[0]) for row in connection.execute(SELECT_POSTGRES_TABLES_SQL).fetchall()
            }
            occupied = sorted({"provider_attempts", SCHEMA_VERSIONS_TABLE} & existing)
            if occupied:
                raise StorageTargetError(
                    f"Refusing to create the metrics schema at {target!r}: "
                    f"{', '.join(repr(name) for name in occupied)} already exists and was left "
                    "untouched. Inspect the target, or choose a different database or schema."
                )
            with connection.transaction():
                connection.execute(CREATE_PROVIDER_ATTEMPTS_TABLE_POSTGRES_SQL)
                connection.execute(CREATE_SCHEMA_VERSIONS_TABLE_POSTGRES_SQL)
                for statement in CREATE_POSTGRES_REQUIRED_METRICS_INDEX_SQL:
                    connection.execute(statement)
                connection.execute(
                    INSERT_METRICS_VERSION_SQL.replace("?", "%s"),
                    (METRICS_COMPONENT, METRICS_SCHEMA_VERSION),
                )
            report = _inspect_connection(connection)
    except Exception as exc:
        if isinstance(exc, StorageAdministrationError):
            raise
        raise StorageAdministrationError(
            f"Could not create the metrics schema at {target!r}. The creating transaction was "
            "rolled back, so no partial schema remains; verify the role's CREATE privilege and "
            "retry."
        ) from exc

    validate_created_schema(report)
    return PostgresCreation(
        target=target,
        metrics_version=METRICS_SCHEMA_VERSION,
        validation=PostgresInspection(target=target, exists=True, schema=report),
    )


def _require_psycopg() -> Any:
    try:
        return importlib.import_module("psycopg")
    except ImportError as exc:
        raise StorageDependencyError(
            'psycopg is not installed. Install it with pip install "nygen-router[postgres]".'
        ) from exc


def _resolve_target(backend: LocalBackend, path: str | Path | None) -> tuple[Path, bool]:
    if path is None:
        if backend is LocalBackend.SQLITE:
            raise StorageTargetError("SQLite administration requires an explicit database path.")
        resolved = default_duckdb_path()
    else:
        resolved = Path(path).expanduser().resolve()
    is_default = backend is LocalBackend.DUCKDB and resolved == default_duckdb_path()
    return resolved, is_default


def _resolve_backup_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.exists():
        raise StorageBackupError(
            f"Refusing backup target {str(resolved)!r}: it already exists and was left untouched."
        )
    return resolved


def _require_duckdb() -> Any:
    try:
        return importlib.import_module("duckdb")
    except ImportError as exc:
        raise StorageDependencyError(
            "DuckDB administration requires the optional dependency: "
            'pip install "nygen-router[duckdb]".'
        ) from exc


def _inspect_sqlite_path(path: Path) -> SchemaReport:
    from nygen_router.storage.sqlite import _inspect_connection

    uri = f"file:{quote(str(path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return _inspect_connection(connection)
    finally:
        connection.close()


def _inspect_duckdb_path(path: Path) -> SchemaReport:
    from nygen_router.storage.duckdb import _inspect_connection

    duckdb = _require_duckdb()
    connection = duckdb.connect(str(path), read_only=True)
    try:
        return _inspect_connection(connection)
    finally:
        connection.close()


def _create_sqlite(path: Path) -> None:
    from nygen_router.storage.sqlite import _inspect_connection

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute(CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
        for sql in CREATE_SQLITE_REQUIRED_METRICS_INDEX_SQL:
            connection.execute(sql)
        connection.execute(CREATE_SCHEMA_VERSIONS_TABLE_SQL)
        connection.execute(INSERT_METRICS_VERSION_SQL, (METRICS_COMPONENT, METRICS_SCHEMA_VERSION))
        validate_created_schema(_inspect_connection(connection))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _create_duckdb(path: Path) -> None:
    from nygen_router.storage.duckdb import _inspect_connection

    duckdb = _require_duckdb()
    connection = duckdb.connect(str(path))
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute(CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
        for sql in CREATE_DUCKDB_REQUIRED_METRICS_INDEX_SQL:
            connection.execute(sql)
        connection.execute(CREATE_SCHEMA_VERSIONS_TABLE_SQL)
        connection.execute(INSERT_METRICS_VERSION_SQL, [METRICS_COMPONENT, METRICS_SCHEMA_VERSION])
        validate_created_schema(_inspect_connection(connection))
        connection.execute("COMMIT")
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def _build_route(inspection: DatabaseInspection) -> tuple[MigrationStep, ...]:
    schema = inspection.schema
    if schema.state is SchemaState.CURRENT:
        return ()
    if schema.state is SchemaState.NEWER:
        raise StorageCompatibilityError(
            f"Cannot migrate {inspection.backend.value} database at {str(inspection.path)!r}: "
            f"detected metrics={schema.metrics_version}, newer than installed "
            f"metrics={METRICS_SCHEMA_VERSION}. The database was left untouched. "
            f"{schema.next_action}"
        )
    if schema.state is SchemaState.SUPPORTED_OLDER:
        route = _numeric_route(schema.metrics_version)
        if route:
            return route
    raise StorageCompatibilityError(
        f"Cannot migrate {inspection.backend.value} database at {str(inspection.path)!r}: "
        f"detected {schema.state.value}; no approved route reaches "
        f"metrics={METRICS_SCHEMA_VERSION}. {schema.detail} The database was left untouched. "
        f"Next safe action: {schema.next_action}"
    )


def _numeric_route(source_version: int | None) -> tuple[MigrationStep, ...]:
    if source_version is None:
        return ()
    route: list[MigrationStep] = []
    version = source_version
    while version < METRICS_SCHEMA_VERSION:
        candidates = [step for step in _MIGRATION_REGISTRY if step.source_version == version]
        if len(candidates) != 1 or candidates[0].target_version != version + 1:
            return ()
        route.append(candidates[0])
        version = candidates[0].target_version
    return tuple(route)


def _migrate_sqlite(
    path: Path,
    planned: tuple[MigrationStep, ...],
    expected_signature: tuple[object, ...],
) -> tuple[MigrationStep, ...]:
    from nygen_router.storage.sqlite import _inspect_connection

    connection = sqlite3.connect(str(path), timeout=0)
    try:
        connection.execute("PRAGMA busy_timeout = 0")
        connection.execute("BEGIN EXCLUSIVE")
        locked_report = DatabaseInspection(
            backend=LocalBackend.SQLITE,
            path=path,
            exists=True,
            is_default_path=False,
            schema=_inspect_connection(connection),
        )
        if _connection_signature(connection, locked_report) != expected_signature:
            raise StorageBusyError(
                f"SQLite database at {str(path)!r} changed after inspection. Stop all writers, "
                "inspect it again, and retry; no migration step was committed."
            )
        completed = _execute_steps(connection, planned)
        validate_created_schema(_inspect_connection(connection))
        connection.commit()
        return completed
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _migrate_duckdb(
    path: Path,
    planned: tuple[MigrationStep, ...],
    expected_signature: tuple[object, ...],
) -> tuple[MigrationStep, ...]:
    from nygen_router.storage.duckdb import _inspect_connection

    duckdb = _require_duckdb()
    connection = duckdb.connect(str(path))
    try:
        connection.execute("BEGIN TRANSACTION")
        locked_report = DatabaseInspection(
            backend=LocalBackend.DUCKDB,
            path=path,
            exists=True,
            is_default_path=path == default_duckdb_path(),
            schema=_inspect_connection(connection),
        )
        if _connection_signature(connection, locked_report) != expected_signature:
            raise StorageBusyError(
                f"DuckDB database at {str(path)!r} changed after inspection. Stop all writers, "
                "inspect it again, and retry; no migration step was committed."
            )
        completed = _execute_steps(connection, planned)
        validate_created_schema(_inspect_connection(connection))
        connection.execute("COMMIT")
        return completed
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def _execute_steps(
    connection: Any, planned: tuple[MigrationStep, ...]
) -> tuple[MigrationStep, ...]:
    if planned:
        step = planned[0]
        raise StorageCompatibilityError(
            f"No executable migration is registered for {step.source_version} -> "
            f"{step.target_version}."
        )
    return ()


def _database_signature(
    backend: LocalBackend, path: Path, inspection: DatabaseInspection
) -> tuple[object, ...]:
    if backend is LocalBackend.SQLITE:
        uri = f"file:{quote(str(path))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    else:
        connection = _require_duckdb().connect(str(path), read_only=True)
    try:
        return _connection_signature(connection, inspection)
    finally:
        connection.close()


def _connection_signature(connection: Any, inspection: DatabaseInspection) -> tuple[object, ...]:
    row_count = int(connection.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0])
    return (
        inspection.schema.state,
        inspection.schema.components,
        inspection.schema.metrics_version,
        row_count,
    )


def _create_validated_backup(
    backend: LocalBackend,
    *,
    source: Path,
    destination: Path,
    expected_signature: tuple[object, ...],
) -> None:
    temporary: Path | None = None
    published = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_database_path(destination)
        if backend is LocalBackend.SQLITE:
            _backup_sqlite(source, temporary)
        else:
            _backup_duckdb(source, temporary)
        backup_inspection = inspect_database(backend, temporary)
        backup_signature = _database_signature(backend, temporary, backup_inspection)
        if backup_signature != expected_signature:
            raise StorageBackupError(
                f"Backup validation did not match the pre-migration source at {str(source)!r}."
            )
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise StorageBackupError(
                f"Refusing backup target {str(destination)!r}: it appeared concurrently and was "
                "left untouched."
            ) from exc
        published = True
        temporary.unlink()
    except BaseException as exc:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        if published and destination.exists():
            destination.unlink()
        if isinstance(exc, StorageBackupError):
            raise
        raise StorageBackupError(
            f"Could not create and validate the requested {backend.value} backup at "
            f"{str(destination)!r}; the source at {str(source)!r} was left untouched."
        ) from exc


def _temporary_database_path(destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=".nygen-router-create-",
        suffix=destination.suffix,
    )
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"file:{quote(str(source))}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True)
    backup_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()


def _backup_duckdb(source: Path, destination: Path) -> None:
    duckdb = _require_duckdb()
    connection = duckdb.connect(":memory:")
    try:
        source_sql = _duckdb_string_literal(str(source))
        destination_sql = _duckdb_string_literal(str(destination))
        connection.execute(f"ATTACH {source_sql} AS nygen_source (READ_ONLY)")
        connection.execute(f"ATTACH {destination_sql} AS nygen_backup")
        connection.execute("COPY FROM DATABASE nygen_source TO nygen_backup")
        connection.execute("CHECKPOINT nygen_backup")
        connection.execute("DETACH nygen_backup")
        connection.execute("DETACH nygen_source")
    finally:
        connection.close()


def _duckdb_string_literal(value: str) -> str:
    """Quote a filesystem path as data for DuckDB statements that cannot bind it."""
    return "'" + value.replace("'", "''") + "'"


def _looks_busy(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message for marker in ("locked", "busy", "conflicting lock", "could not set lock")
    )
