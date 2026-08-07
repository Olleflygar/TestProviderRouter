from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

SCHEMA_VERSIONS_TABLE = "nygen_router_schema_versions"
METRICS_COMPONENT = "metrics"
IMPLICIT_METRICS_SCHEMA_VERSION = 1
METRICS_SCHEMA_VERSION = 2

CREATE_PROVIDER_ATTEMPTS_TABLE_SQL = """
CREATE TABLE provider_attempts (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    metrics_scope TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model TEXT NOT NULL,
    protocol TEXT NOT NULL,
    call_type TEXT NOT NULL,
    success INTEGER NOT NULL,
    stream_opened INTEGER,
    latency_ms REAL,
    total_duration_ms REAL,
    error_type TEXT
)
"""

CREATE_SCHEMA_VERSIONS_TABLE_SQL = f"""
CREATE TABLE {SCHEMA_VERSIONS_TABLE} (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL
)
"""

# The same logical schema in PostgreSQL's native types. Operators who run their
# own migration tooling can apply these two statements directly instead of
# using the storage administration surface.
CREATE_PROVIDER_ATTEMPTS_TABLE_POSTGRES_SQL = """
CREATE TABLE provider_attempts (
    id TEXT PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    metrics_scope TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model TEXT NOT NULL,
    protocol TEXT NOT NULL,
    call_type TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    stream_opened BOOLEAN,
    latency_ms DOUBLE PRECISION,
    total_duration_ms DOUBLE PRECISION,
    error_type TEXT
)
"""

CREATE_SCHEMA_VERSIONS_TABLE_POSTGRES_SQL = f"""
CREATE TABLE {SCHEMA_VERSIONS_TABLE} (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL
)
"""

COLUMN_NAMES = (
    "id",
    "timestamp",
    "metrics_scope",
    "provider_id",
    "provider_name",
    "model",
    "protocol",
    "call_type",
    "success",
    "stream_opened",
    "latency_ms",
    "total_duration_ms",
    "error_type",
)

TABLE_INFO_PROVIDER_ATTEMPTS_SQL = "PRAGMA table_info('provider_attempts')"
TABLE_INFO_SCHEMA_VERSIONS_SQL = f"PRAGMA table_info('{SCHEMA_VERSIONS_TABLE}')"
SELECT_SCHEMA_VERSIONS_SQL = (
    f"SELECT component, version FROM {SCHEMA_VERSIONS_TABLE} ORDER BY component"
)
INSERT_METRICS_VERSION_SQL = (
    f"INSERT INTO {SCHEMA_VERSIONS_TABLE} (component, version) VALUES (?, ?)"
)


@dataclass(frozen=True)
class IndexDefinition:
    """Normalized definition of one project-owned persistent metrics index."""

    name: str
    table: str
    unique: bool
    columns: tuple[str, ...]

    @property
    def create_sql(self) -> str:
        unique = "UNIQUE " if self.unique else ""
        columns = ", ".join(self.columns)
        return f"CREATE {unique}INDEX {self.name} ON {self.table} ({columns})"


# The 60,000-row/9-result/7-repetition PR30 benchmark showed SQLite using this
# index for both current- and all-scope joins (1.240208/2.090667 ms medians,
# versus 43.191167/43.610417 ms unindexed). Keeping scope out of the leading
# columns lets one index serve both plans. A second scope-leading index reached
# 0.556125 ms for current scope but was rejected for write/storage cost.
# DuckDB 1.5.5 used sequential scans with no indexes (8.648875/9.006959 ms) and
# two ART candidates (8.202791/9.003167 ms), with higher indexed seed/storage
# cost, so its smallest measured set is empty. Timings are one-machine evidence,
# not a universal latency guarantee.
SQLITE_REQUIRED_METRICS_INDEXES = (
    IndexDefinition(
        name="provider_attempts_partition_timestamp_idx",
        table="provider_attempts",
        unique=False,
        columns=("provider_id", "model", "protocol", "call_type", "timestamp"),
    ),
)
DUCKDB_REQUIRED_METRICS_INDEXES: tuple[IndexDefinition, ...] = ()
CREATE_SQLITE_REQUIRED_METRICS_INDEX_SQL = tuple(
    definition.create_sql for definition in SQLITE_REQUIRED_METRICS_INDEXES
)
CREATE_DUCKDB_REQUIRED_METRICS_INDEX_SQL = tuple(
    definition.create_sql for definition in DUCKDB_REQUIRED_METRICS_INDEXES
)

# Measured against PostgreSQL 17.6 with 60,000 rows and 9 requested providers
# over 7 repetitions; see benchmarks/pr14a_postgres_score_aggregation.py. Every
# current-scope, all-scope, and exponential query moved from a sequential scan
# to an index scan on this definition, with the planner's estimated scan cost
# falling from about 2100-2250 to about 157-179. As with SQLite, keeping scope
# out of the leading columns lets one index serve both scope plans.
#
# Wall-clock medians barely moved (161 -> 153 ms) because that run went to a
# managed database whose network round trip is roughly 150 ms and therefore
# dominates the measurement. The access path and the bounded 9-row result are
# the durable evidence; the timings are one-machine context, not a promise.
POSTGRES_REQUIRED_METRICS_INDEXES = (
    IndexDefinition(
        name="provider_attempts_partition_timestamp_idx",
        table="provider_attempts",
        unique=False,
        columns=("provider_id", "model", "protocol", "call_type", "timestamp"),
    ),
)
CREATE_POSTGRES_REQUIRED_METRICS_INDEX_SQL = tuple(
    definition.create_sql for definition in POSTGRES_REQUIRED_METRICS_INDEXES
)

# PostgreSQL catalog reads. Every one is read-only and schema-qualified to the
# connection's own search_path via current_schema(), so inspection never
# reaches into another application's tables.
SELECT_POSTGRES_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = current_schema() ORDER BY table_name"
)
SELECT_POSTGRES_COLUMNS_SQL = """
SELECT
    ordinal_position - 1,
    column_name,
    data_type,
    CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END,
    NULL,
    CASE WHEN EXISTS (
        SELECT 1
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
            AND tc.table_schema = c.table_schema
            AND tc.table_name = c.table_name
            AND kcu.column_name = c.column_name
    ) THEN 1 ELSE 0 END
FROM information_schema.columns c
WHERE table_schema = current_schema() AND table_name = %s
ORDER BY ordinal_position
"""
SELECT_POSTGRES_INDEXES_SQL = """
SELECT
    i.relname,
    ix.indisunique,
    a.attname,
    k.ordinality
FROM pg_class t
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ordinality) ON TRUE
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE n.nspname = current_schema() AND t.relname = 'provider_attempts'
ORDER BY i.relname, k.ordinality
"""

# PostgreSQL stores the same logical columns in its own native types, so the
# expected physical shape differs from the local text/integer representation
# even though the behavioral contract is identical.
_EXPECTED_POSTGRES_PROVIDER_SCHEMA = (
    ("id", "text", False, True),
    ("timestamp", "timestamptz", True, False),
    ("metrics_scope", "text", True, False),
    ("provider_id", "text", True, False),
    ("provider_name", "text", True, False),
    ("model", "text", True, False),
    ("protocol", "text", True, False),
    ("call_type", "text", True, False),
    ("success", "boolean", True, False),
    ("stream_opened", "boolean", False, False),
    ("latency_ms", "real", False, False),
    ("total_duration_ms", "real", False, False),
    ("error_type", "text", False, False),
)

_EXPECTED_PROVIDER_SCHEMA = (
    ("id", "text", False, True),
    ("timestamp", "text", True, False),
    ("metrics_scope", "text", True, False),
    ("provider_id", "text", True, False),
    ("provider_name", "text", True, False),
    ("model", "text", True, False),
    ("protocol", "text", True, False),
    ("call_type", "text", True, False),
    ("success", "integer", True, False),
    ("stream_opened", "integer", False, False),
    ("latency_ms", "real", False, False),
    ("total_duration_ms", "real", False, False),
    ("error_type", "text", False, False),
)

_EXPECTED_VERSION_SCHEMA = (
    ("component", "text", False, True),
    ("version", "integer", True, False),
)


class SchemaState(StrEnum):
    """The supported and unsupported states of one local metrics database."""

    MISSING = "missing"
    IMPLICIT_BASELINE = "implicit_unversioned_v1"
    CURRENT = "current"
    SUPPORTED_OLDER = "supported_older"
    UNKNOWN = "unsupported_or_unknown"
    NEWER = "newer_than_installed"


class MetadataState(StrEnum):
    """Whether component-version metadata is absent, valid, or malformed."""

    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class ComponentVersion:
    component: str
    version: int


@dataclass(frozen=True)
class SchemaReport:
    """Storage-neutral result of inspecting local database catalog rows."""

    state: SchemaState
    metadata_state: MetadataState
    components: tuple[ComponentVersion, ...]
    metrics_version: int | None
    compatible: bool
    next_action: str
    detail: str


class MetricsSchemaMismatchError(RuntimeError):
    """An existing metrics database is not safe for normal runtime use."""


def missing_schema_report() -> SchemaReport:
    return SchemaReport(
        state=SchemaState.MISSING,
        metadata_state=MetadataState.ABSENT,
        components=(),
        metrics_version=None,
        compatible=False,
        next_action=(
            "Create the database explicitly, or let the selected local store create it on first "
            "use."
        ),
        detail="The target does not exist.",
    )


def inspect_schema_rows(
    *,
    table_names: Sequence[object],
    provider_schema_rows: Sequence[Sequence[Any]],
    version_schema_rows: Sequence[Sequence[Any]],
    version_rows: Sequence[Sequence[Any]],
    index_definitions: Sequence[IndexDefinition],
    required_indexes: Sequence[IndexDefinition],
    expected_provider_schema: Sequence[tuple[str, str, bool, bool]] = _EXPECTED_PROVIDER_SCHEMA,
    allow_implicit_baseline: bool = True,
) -> SchemaReport:
    """Classify catalog data gathered read-only by a concrete backend."""
    tables = {str(name[0] if isinstance(name, (tuple, list)) else name) for name in table_names}
    has_provider = "provider_attempts" in tables
    has_versions = SCHEMA_VERSIONS_TABLE in tables

    provider_error = _schema_error(provider_schema_rows, expected_provider_schema)
    if not has_versions:
        if has_provider and provider_error is None and allow_implicit_baseline:
            return SchemaReport(
                state=SchemaState.IMPLICIT_BASELINE,
                metadata_state=MetadataState.ABSENT,
                components=(),
                metrics_version=IMPLICIT_METRICS_SCHEMA_VERSION,
                compatible=False,
                next_action=(
                    "Archive/delete this disposable version-1 target while the application is "
                    "stopped, or configure a different absent path for a fresh version-2 database."
                ),
                detail=(
                    "The exact unversioned PR29 metrics schema is an implicit version-1 baseline; "
                    "PR30 provides no migration route to version 2."
                ),
            )
        detail = (
            "The provider_attempts table is missing."
            if not has_provider
            else f"The provider_attempts table is incompatible: {provider_error}."
        )
        return _unknown_report(MetadataState.ABSENT, (), None, detail)

    version_error = _schema_error(version_schema_rows, _EXPECTED_VERSION_SCHEMA)
    components, component_error = _component_versions(version_rows)
    metrics_version = next(
        (item.version for item in components if item.component == METRICS_COMPONENT), None
    )
    if version_error is not None:
        return _unknown_report(
            MetadataState.INVALID,
            components,
            metrics_version,
            (
                f"The {SCHEMA_VERSIONS_TABLE} table is incompatible: {version_error}."
                if component_error is None
                else (
                    f"The {SCHEMA_VERSIONS_TABLE} table is incompatible: {version_error}; "
                    f"{component_error}"
                )
            ),
        )
    if component_error is not None:
        return _unknown_report(MetadataState.INVALID, components, None, component_error)
    if metrics_version is None:
        return _unknown_report(
            MetadataState.VALID,
            components,
            None,
            "Version metadata has no required metrics component row.",
        )
    if metrics_version > METRICS_SCHEMA_VERSION:
        return SchemaReport(
            state=SchemaState.NEWER,
            metadata_state=MetadataState.VALID,
            components=components,
            metrics_version=metrics_version,
            compatible=False,
            next_action="Use a llm-provider-router version that supports this newer metrics schema.",
            detail=(
                f"The database records metrics version {metrics_version}, newer than installed "
                f"version {METRICS_SCHEMA_VERSION}."
            ),
        )
    if metrics_version < METRICS_SCHEMA_VERSION:
        return SchemaReport(
            state=SchemaState.SUPPORTED_OLDER,
            metadata_state=MetadataState.VALID,
            components=components,
            metrics_version=metrics_version,
            compatible=False,
            next_action=(
                "Archive/delete this disposable version-1 target while the application is "
                "stopped, or configure a different absent path for a fresh version-2 database."
            ),
            detail=(
                f"The database records supported metrics version {metrics_version}; installed "
                f"version is {METRICS_SCHEMA_VERSION}, and no approved migration route exists."
            ),
        )
    if not has_provider or provider_error is not None:
        detail = (
            "Version metadata claims the current metrics version, but provider_attempts is missing."
            if not has_provider
            else (
                "Version metadata claims the current metrics version, but provider_attempts is "
                f"incompatible: {provider_error}."
            )
        )
        return _unknown_report(MetadataState.VALID, components, metrics_version, detail)
    index_error = _required_index_error(index_definitions, required_indexes)
    if index_error is not None:
        return _unknown_report(
            MetadataState.VALID,
            components,
            metrics_version,
            (
                "Version metadata claims the current metrics version, but required "
                f"project-owned indexes are incompatible: {index_error}."
            ),
        )
    return SchemaReport(
        state=SchemaState.CURRENT,
        metadata_state=MetadataState.VALID,
        components=components,
        metrics_version=metrics_version,
        compatible=True,
        next_action="No schema action is required.",
        detail="The metrics schema and component-version metadata are current.",
    )


def validate_runtime_schema(report: SchemaReport, *, backend: str, path: str) -> None:
    """Permit only a fully validated current version-2 database at runtime."""
    if report.state is SchemaState.CURRENT:
        return
    detected = (
        report.state.value
        if report.metrics_version is None
        else f"{report.state.value} (metrics={report.metrics_version})"
    )
    raise MetricsSchemaMismatchError(
        f"{backend} metrics database at {path!r} is incompatible: detected {detected}; "
        f"expected metrics={METRICS_SCHEMA_VERSION}. {report.detail} The database was left "
        f"untouched. Next safe action: {report.next_action}"
    )


def validate_created_schema(report: SchemaReport) -> None:
    if report.state is not SchemaState.CURRENT:
        raise MetricsSchemaMismatchError(
            "New database validation failed: expected the current metrics schema and "
            f"metrics={METRICS_SCHEMA_VERSION}, found {report.state.value}."
        )


def _unknown_report(
    metadata_state: MetadataState,
    components: tuple[ComponentVersion, ...],
    metrics_version: int | None,
    detail: str,
) -> SchemaReport:
    return SchemaReport(
        state=SchemaState.UNKNOWN,
        metadata_state=metadata_state,
        components=components,
        metrics_version=metrics_version,
        compatible=False,
        next_action=(
            "Preserve or manually export any needed data, then either move/archive the file while "
            "the application is stopped or choose and configure a different absent path."
        ),
        detail=detail,
    )


def _required_index_error(
    index_definitions: Sequence[IndexDefinition],
    required_indexes: Sequence[IndexDefinition],
) -> str | None:
    by_name: dict[str, IndexDefinition] = {}
    for definition in index_definitions:
        if definition.name in by_name:
            return f"catalog inspection returned duplicate index {definition.name!r}"
        by_name[definition.name] = definition
    for expected in required_indexes:
        actual = by_name.get(expected.name)
        if actual is None:
            return f"missing required index {expected.name!r}"
        if actual != expected:
            return (
                f"index {expected.name!r} has table={actual.table!r}, "
                f"unique={actual.unique!r}, columns={actual.columns!r}; expected "
                f"table={expected.table!r}, unique={expected.unique!r}, "
                f"columns={expected.columns!r}"
            )
    return None


def _component_versions(
    rows: Sequence[Sequence[Any]],
) -> tuple[tuple[ComponentVersion, ...], str | None]:
    components: list[ComponentVersion] = []
    seen: set[str] = set()
    for row in rows:
        if len(row) != 2:
            return tuple(components), "Version metadata contains a malformed component record."
        component, raw_version = row
        if (
            not isinstance(component, str)
            or not component.strip()
            or component != component.strip()
        ):
            return tuple(components), "Version metadata contains an invalid component name."
        if component in seen:
            return (
                tuple(components),
                f"Version metadata contains duplicate component {component!r}.",
            )
        if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version <= 0:
            return (
                tuple(components),
                f"Version metadata for component {component!r} must be a positive integer.",
            )
        seen.add(component)
        components.append(ComponentVersion(component=component, version=raw_version))
    return tuple(components), None


def _schema_error(
    rows: Sequence[Sequence[Any]], expected: Sequence[tuple[str, str, bool, bool]]
) -> str | None:
    actual: list[tuple[str, str, bool, bool, object]] = []
    for row in rows:
        if len(row) < 6:
            return "catalog inspection returned malformed column metadata"
        name = str(row[1])
        logical_type = _logical_type(str(row[2]))
        not_null = bool(row[3])
        default = row[4]
        primary_key = bool(row[5])
        if primary_key:
            not_null = False
        actual.append((name, logical_type, not_null, primary_key, default))
    expected_with_defaults = [(*column, None) for column in expected]
    if actual == expected_with_defaults:
        return None
    actual_names = ", ".join(item[0] for item in actual) or "<none>"
    expected_names = ", ".join(item[0] for item in expected)
    return f"found columns [{actual_names}], expected exactly [{expected_names}]"


def _logical_type(value: str) -> str:
    normalized = value.upper()
    if normalized in {"TEXT", "VARCHAR", "CHARACTER VARYING"}:
        return "text"
    if normalized in {"INTEGER", "INT", "BIGINT"}:
        return "integer"
    if normalized in {"REAL", "FLOAT", "DOUBLE", "DOUBLE PRECISION"}:
        return "real"
    if normalized in {"BOOLEAN", "BOOL"}:
        return "boolean"
    if normalized in {"TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"}:
        return "timestamptz"
    return normalized.lower()


def inspect_postgres_schema_rows(
    *,
    table_names: Sequence[object],
    provider_schema_rows: Sequence[Sequence[Any]],
    version_schema_rows: Sequence[Sequence[Any]],
    version_rows: Sequence[Sequence[Any]],
    index_definitions: Sequence[IndexDefinition],
) -> SchemaReport:
    """Classify PostgreSQL catalog data against the native-type expectations.

    PostgreSQL never had a version-1 schema, so a provider_attempts table with
    no version metadata is a foreign or half-created table rather than an
    implicit baseline.
    """
    tables = {str(name[0] if isinstance(name, (tuple, list)) else name) for name in table_names}
    if not tables & {"provider_attempts", SCHEMA_VERSIONS_TABLE}:
        # A reachable database with none of our tables is un-provisioned, not
        # damaged: the safe next step is to create the schema, not to rescue
        # data. The local backends reach this state by finding no file.
        return SchemaReport(
            state=SchemaState.MISSING,
            metadata_state=MetadataState.ABSENT,
            components=(),
            metrics_version=None,
            compatible=False,
            next_action=(
                "Provision the schema deliberately with a schema-owning role: run "
                "llm-provider-router storage create --backend postgres, or apply the published "
                "CREATE TABLE statements with your own migration tooling. The router never "
                "creates a remote schema."
            ),
            detail="The database is reachable but holds no llm-provider-router metrics schema.",
        )
    return inspect_schema_rows(
        table_names=table_names,
        provider_schema_rows=provider_schema_rows,
        version_schema_rows=version_schema_rows,
        version_rows=version_rows,
        index_definitions=index_definitions,
        required_indexes=POSTGRES_REQUIRED_METRICS_INDEXES,
        expected_provider_schema=_EXPECTED_POSTGRES_PROVIDER_SCHEMA,
        allow_implicit_baseline=False,
    )
