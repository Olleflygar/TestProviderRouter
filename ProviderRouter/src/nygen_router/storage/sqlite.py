from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from nygen_router.config import ApiProtocol
from nygen_router.metrics import MetricsEvent
from nygen_router.storage.base import (
    INSERT_PROVIDER_ATTEMPT_SQL,
    build_query_recent_sql,
    event_to_params,
    row_to_event,
)
from nygen_router.storage.schema import (
    SCHEMA_VERSIONS_TABLE,
    SELECT_SCHEMA_VERSIONS_SQL,
    TABLE_INFO_PROVIDER_ATTEMPTS_SQL,
    TABLE_INFO_SCHEMA_VERSIONS_SQL,
    MetricsSchemaMismatchError,
    SchemaReport,
    inspect_schema_rows,
    validate_runtime_schema,
)
from nygen_router.types import CallType


class SQLiteMetricsStore:
    """Stdlib sqlite3-backed MetricsStore -- no optional dependency required.

    The recommended option when several local processes must share one store:
    SQLite handles cross-process file locking natively, unlike DuckDB.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    def record_attempt(self, event: MetricsEvent) -> None:
        params = event_to_params(event)
        connection = self._connect()
        connection.execute(INSERT_PROVIDER_ATTEMPT_SQL, params)
        connection.commit()

    def query_recent(
        self,
        *,
        since: datetime,
        metrics_scope: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        protocol: ApiProtocol | None = None,
        call_type: CallType | None = None,
    ) -> list[MetricsEvent]:
        query, params = build_query_recent_sql(
            since=since,
            metrics_scope=metrics_scope,
            provider_id=provider_id,
            model=model,
            protocol=protocol,
            call_type=call_type,
        )
        connection = self._connect()
        rows = connection.execute(query, params).fetchall()
        return [row_to_event(row) for row in rows]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            existed = self.path.exists()
            if existed:
                report = _inspect_existing(self.path)
                validate_runtime_schema(report, backend="SQLite", path=str(self.path.resolve()))
            else:
                from nygen_router.storage.admin import LocalBackend, create_database

                create_database(LocalBackend.SQLITE, self.path)
            connection = sqlite3.connect(str(self.path))
            self._connection = connection
        return self._connection


def _inspect_existing(path: Path) -> SchemaReport:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise MetricsSchemaMismatchError(
            f"SQLite metrics database at {str(path.resolve())!r} could not be inspected safely. "
            "The existing target was left untouched; inspect it with the storage administration "
            "command or choose and configure a different absent path."
        ) from exc
    try:
        return _inspect_connection(connection)
    except sqlite3.Error as exc:
        raise MetricsSchemaMismatchError(
            f"SQLite metrics database at {str(path.resolve())!r} could not be inspected safely. "
            "The existing target was left untouched; inspect it with the storage administration "
            "command or choose and configure a different absent path."
        ) from exc
    finally:
        connection.close()


def _inspect_connection(connection: sqlite3.Connection) -> SchemaReport:
    table_names = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    tables = {str(row[0]) for row in table_names}
    provider_rows = (
        connection.execute(TABLE_INFO_PROVIDER_ATTEMPTS_SQL).fetchall()
        if "provider_attempts" in tables
        else []
    )
    version_schema_rows = (
        connection.execute(TABLE_INFO_SCHEMA_VERSIONS_SQL).fetchall()
        if SCHEMA_VERSIONS_TABLE in tables
        else []
    )
    version_columns = tuple(str(row[1]) for row in version_schema_rows)
    version_rows = (
        connection.execute(SELECT_SCHEMA_VERSIONS_SQL).fetchall()
        if {"component", "version"}.issubset(version_columns)
        else []
    )
    return inspect_schema_rows(
        table_names=table_names,
        provider_schema_rows=provider_rows,
        version_schema_rows=version_schema_rows,
        version_rows=version_rows,
    )
