from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from llm_provider_router.storage.admin import (
    DatabaseCreation,
    DatabaseInspection,
    DatabaseMigration,
    LocalBackend,
    MigrationStep,
    PostgresCreation,
    PostgresInspection,
    StorageAdministrationError,
    StorageBackupError,
    StorageBusyError,
    StorageCompatibilityError,
    StorageDependencyError,
    StorageTargetError,
    create_database,
    create_postgres_database,
    inspect_database,
    inspect_postgres_database,
    migrate_database,
)

POSTGRES_BACKEND = "postgres"
POSTGRES_URL_ENV = "NYGEN_ROUTER_POSTGRES_URL"

EXIT_INVALID_ARGUMENTS = 2
EXIT_DEPENDENCY = 3
EXIT_INCOMPATIBLE = 4
EXIT_BUSY = 5
EXIT_ADMINISTRATION = 6
EXIT_UNEXPECTED = 7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-provider-router")
    commands = parser.add_subparsers(dest="command", required=True)
    storage = commands.add_parser(
        "storage",
        help="inspect/administer local metrics v2 storage; v1 has no PR30 migration",
    )
    operations = storage.add_subparsers(dest="storage_command", required=True)
    for operation, help_text in (
        ("inspect", "inspect a database without changing it"),
        ("create", "create a new metrics v2 database; never overwrite"),
        (
            "migrate",
            "run a registered offline route; no v1/implicit-v1 to v2 route exists",
        ),
    ):
        subparser = operations.add_parser(operation, help=help_text)
        subparser.add_argument(
            "--backend",
            required=True,
            choices=(*(item.value for item in LocalBackend), POSTGRES_BACKEND),
        )
        target = subparser.add_mutually_exclusive_group()
        target.add_argument("--path", type=Path, help="explicit local database path")
        target.add_argument(
            "--default",
            action="store_true",
            help="use the router's standard DuckDB path (DuckDB only)",
        )
        target.add_argument(
            "--url",
            help=(
                "PostgreSQL connection string (postgres only); defaults to the "
                f"{POSTGRES_URL_ENV} environment variable, which keeps credentials out of "
                "shell history and the process list"
            ),
        )
        if operation == "migrate":
            subparser.add_argument(
                "--backup", type=Path, help="optional absent destination for a pre-migration backup"
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.backend == POSTGRES_BACKEND:
        return _run_postgres(parser, args)

    backend = LocalBackend(args.backend)
    if args.url is not None:
        parser.error("--url is available only with --backend postgres")
    if args.path is None and not args.default:
        parser.error("one of --path or --default is required for a local backend")
    if args.default and backend is not LocalBackend.DUCKDB:
        parser.error("--default is available only with --backend duckdb")
    path = None if args.default else args.path
    try:
        if args.storage_command == "inspect":
            _print_inspection(inspect_database(backend, path))
        elif args.storage_command == "create":
            _print_creation(create_database(backend, path))
        else:
            _print_migration(migrate_database(backend, path, backup_path=args.backup))
    except StorageDependencyError as exc:
        _print_error(exc)
        return EXIT_DEPENDENCY
    except (StorageCompatibilityError, StorageTargetError, StorageBackupError) as exc:
        _print_error(exc)
        return EXIT_INCOMPATIBLE
    except StorageBusyError as exc:
        _print_error(exc)
        print(
            "Recovery: the source was not partially migrated; stop all writers, inspect it, "
            "and retry. Keep any successfully validated backup.",
            file=sys.stderr,
        )
        return EXIT_BUSY
    except StorageAdministrationError as exc:
        _print_error(exc)
        print(
            "Recovery: inspect the unchanged or rolled-back source before retrying, and keep any "
            "successfully validated backup.",
            file=sys.stderr,
        )
        return EXIT_ADMINISTRATION
    except Exception:
        print(
            "Error: unexpected storage administration failure. The command did not intentionally "
            "replace or redirect the selected database; inspect the source before retrying.",
            file=sys.stderr,
        )
        return EXIT_UNEXPECTED
    return 0


def _run_postgres(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.path is not None or args.default:
        parser.error("--path and --default are local-backend options; use --url for postgres")
    url = args.url or os.environ.get(POSTGRES_URL_ENV)
    if not url:
        parser.error(
            f"--backend postgres requires --url or the {POSTGRES_URL_ENV} environment variable"
        )
    if args.storage_command == "migrate":
        print(
            "Error: no PostgreSQL migration route is registered. The metrics schema has a "
            "single version; provision it with 'create' and apply future schema changes "
            "through your deployment tooling.",
            file=sys.stderr,
        )
        return EXIT_INCOMPATIBLE
    try:
        if args.storage_command == "inspect":
            _print_postgres_inspection(inspect_postgres_database(url))
        else:
            _print_postgres_creation(create_postgres_database(url))
    except StorageDependencyError as exc:
        _print_error(exc)
        return EXIT_DEPENDENCY
    except (StorageCompatibilityError, StorageTargetError) as exc:
        _print_error(exc)
        return EXIT_INCOMPATIBLE
    except StorageAdministrationError as exc:
        _print_error(exc)
        print(
            "Recovery: the creating transaction is rolled back on failure, so no partial "
            "schema remains; inspect the target before retrying.",
            file=sys.stderr,
        )
        return EXIT_ADMINISTRATION
    except Exception:
        print(
            "Error: unexpected storage administration failure. The command did not "
            "intentionally alter the selected database; inspect the target before retrying.",
            file=sys.stderr,
        )
        return EXIT_UNEXPECTED
    return 0


def _print_postgres_inspection(result: PostgresInspection) -> None:
    print("Backend: postgres")
    print(f"Target: {result.target}")
    print(f"Schema present: {_yes_no(result.exists)}")
    print(f"Schema state: {result.schema.state.value}")
    print(f"Metadata table: {result.schema.metadata_state.value}")
    components = ", ".join(f"{item.component}={item.version}" for item in result.schema.components)
    print(f"Component versions: {components or '<none>'}")
    print(f"Runtime compatible: {_yes_no(result.schema.compatible)}")
    print(f"Detail: {result.schema.detail}")
    print(f"Next safe action: {result.schema.next_action}")


def _print_postgres_creation(result: PostgresCreation) -> None:
    print(f"Created postgres metrics schema: {result.target}")
    print(f"Latest metrics version: {result.metrics_version}")
    print(f"Validation: {result.validation.schema.state.value}")
    print(
        "Grant the application's runtime role only INSERT/SELECT on provider_attempts and "
        "SELECT on nygen_router_schema_versions; the router never alters this schema."
    )
    print("from llm_provider_router import PostgresMetricsStore, ProviderRouter")
    print("metrics_store = PostgresMetricsStore(url)")
    print("router = ProviderRouter(..., metrics_store=metrics_store)")


def _print_inspection(result: DatabaseInspection) -> None:
    print(f"Backend: {result.backend.value}")
    print(f"Path: {result.path}")
    print(f"Exists: {_yes_no(result.exists)}")
    print(f"Router default path: {_yes_no(result.is_default_path)}")
    print(f"Schema state: {result.schema.state.value}")
    print(f"Metadata table: {result.schema.metadata_state.value}")
    components = ", ".join(f"{item.component}={item.version}" for item in result.schema.components)
    print(f"Component versions: {components or '<none>'}")
    print(f"Runtime compatible: {_yes_no(result.schema.compatible)}")
    print(f"Detail: {result.schema.detail}")
    print(f"Next safe action: {result.schema.next_action}")


def _print_creation(result: DatabaseCreation) -> None:
    print(f"Created {result.backend.value} database: {result.path}")
    print(f"Latest metrics version: {result.metrics_version}")
    print(f"Validation: {result.validation.schema.state.value}")
    print(f"Router default path: {_yes_no(result.is_default_path)}")
    if result.is_default_path:
        print(
            "The default ProviderRouter metrics store will reuse this path; no explicit path "
            "is needed."
        )
        return
    store_name = (
        "DuckDBMetricsStore" if result.backend is LocalBackend.DUCKDB else "SQLiteMetricsStore"
    )
    print(
        "ProviderRouter will not discover this non-default database automatically. Configure the "
        "concrete store path explicitly:"
    )
    print(f"from llm_provider_router import {store_name}, ProviderRouter")
    print(f"metrics_store = {store_name}({str(result.path)!r})")
    print("router = ProviderRouter(..., metrics_store=metrics_store)")


def _print_migration(result: DatabaseMigration) -> None:
    source = (
        "implicit unversioned baseline"
        if result.source_version is None
        else str(result.source_version)
    )
    print(f"Migrated {result.backend.value} database: {result.path}")
    print(f"Source metrics version: {source}")
    print(f"Target metrics version: {result.target_version}")
    print(f"Planned steps: {_format_steps(result.planned_steps)}")
    print(f"Completed steps: {_format_steps(result.completed_steps)}")
    print(f"No-op: {_yes_no(result.no_op)}")
    print(f"Backup: {result.backup_path if result.backup_path is not None else '<none>'}")
    print(f"Final validation: {result.validation.schema.state.value}")


def _format_steps(steps: Sequence[MigrationStep]) -> str:
    names = [step.name for step in steps]
    return ", ".join(names) or "<none>"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _print_error(exc: BaseException) -> None:
    print(f"Error: {exc}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover - console-script and subprocess boundary
    raise SystemExit(main())
