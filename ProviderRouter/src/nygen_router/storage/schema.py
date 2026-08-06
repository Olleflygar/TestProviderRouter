from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

SCHEMA_VERSIONS_TABLE = "nygen_router_schema_versions"
METRICS_COMPONENT = "metrics"
METRICS_SCHEMA_VERSION = 1

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
) -> SchemaReport:
    """Classify catalog data gathered read-only by a concrete backend."""
    tables = {str(name[0] if isinstance(name, (tuple, list)) else name) for name in table_names}
    has_provider = "provider_attempts" in tables
    has_versions = SCHEMA_VERSIONS_TABLE in tables

    provider_error = _schema_error(provider_schema_rows, _EXPECTED_PROVIDER_SCHEMA)
    if not has_versions:
        if has_provider and provider_error is None:
            return SchemaReport(
                state=SchemaState.IMPLICIT_BASELINE,
                metadata_state=MetadataState.ABSENT,
                components=(),
                metrics_version=METRICS_SCHEMA_VERSION,
                compatible=True,
                next_action=(
                    "Normal runtime may reuse this exact baseline without changing it; run the "
                    "offline migrate command to stamp metrics version 1 explicitly."
                ),
                detail="The exact metrics version-1 schema exists without version metadata.",
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
            next_action="Use a nygen-router version that supports this newer metrics schema.",
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
            next_action="Stop all writers and run the explicit offline migrate command.",
            detail=(
                f"The database records supported metrics version {metrics_version}; installed "
                f"version is {METRICS_SCHEMA_VERSION}."
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
    """Permit only current or exact implicit-v1 databases during normal runtime."""
    if report.state in {SchemaState.CURRENT, SchemaState.IMPLICIT_BASELINE}:
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
    if normalized in {"TEXT", "VARCHAR"}:
        return "text"
    if normalized in {"INTEGER", "INT", "BIGINT"}:
        return "integer"
    if normalized in {"REAL", "FLOAT", "DOUBLE"}:
        return "real"
    return normalized.lower()
