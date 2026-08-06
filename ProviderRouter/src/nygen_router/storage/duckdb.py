from __future__ import annotations

import importlib.util
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger(__name__)


class DuckDBMetricsStore:
    """DuckDB-backed MetricsStore -- the embedded, no-server-to-run default.

    Single-process by design: DuckDB allows one writing process per file. Do
    not build cross-process coordination here -- users who need several local
    processes sharing one store should use SQLiteMetricsStore instead.
    """

    def __init__(
        self, path: str | Path | None = None, *, sdk_available: bool | None = None
    ) -> None:
        self.path = (
            Path(path) if path is not None else Path.home() / ".nygen_router" / "metrics.duckdb"
        )
        available = (
            importlib.util.find_spec("duckdb") is not None
            if sdk_available is None
            else sdk_available
        )
        if not available:
            logger.warning(
                "duckdb is not installed: metrics persistence will not work. Run "
                'pip install "nygen-router[duckdb]", or pass a different metrics_store '
                "to ProviderRouter."
            )
        self._sdk_available = available
        self._connection: duckdb.DuckDBPyConnection | None = None

    @property
    def available(self) -> bool:
        """Whether the optional DuckDB dependency was present at construction."""
        return self._sdk_available

    def record_attempt(self, event: MetricsEvent) -> None:
        params = event_to_params(event)
        connection = self._connect()
        connection.execute(INSERT_PROVIDER_ATTEMPT_SQL, list(params))

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
        rows: list[Any] = connection.execute(query, params).fetchall()
        return [row_to_event(row) for row in rows]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if not self._sdk_available:
            raise ImportError(
                'duckdb is not installed; install it with pip install "nygen-router[duckdb]"'
            )
        if self._connection is None:
            import duckdb

            existed = self.path.exists()
            if existed:
                try:
                    inspection = duckdb.connect(str(self.path), read_only=True)
                    try:
                        report = _inspect_connection(inspection)
                    finally:
                        inspection.close()
                except Exception as exc:
                    if isinstance(exc, MetricsSchemaMismatchError):
                        raise
                    raise MetricsSchemaMismatchError(
                        f"DuckDB metrics database at {str(self.path.resolve())!r} could not be "
                        "inspected safely. The existing target was left untouched; inspect it "
                        "with the storage administration command or choose and configure a "
                        "different absent path."
                    ) from exc
                validate_runtime_schema(report, backend="DuckDB", path=str(self.path.resolve()))
            else:
                from nygen_router.storage.admin import LocalBackend, create_database

                create_database(LocalBackend.DUCKDB, self.path)
            connection = duckdb.connect(str(self.path))
            self._connection = connection
        return self._connection


def _inspect_connection(connection: duckdb.DuckDBPyConnection) -> SchemaReport:
    table_names: list[Any] = connection.execute("SHOW TABLES").fetchall()
    tables = {str(row[0]) for row in table_names}
    provider_rows: list[Any] = (
        connection.execute(TABLE_INFO_PROVIDER_ATTEMPTS_SQL).fetchall()
        if "provider_attempts" in tables
        else []
    )
    version_schema_rows: list[Any] = (
        connection.execute(TABLE_INFO_SCHEMA_VERSIONS_SQL).fetchall()
        if SCHEMA_VERSIONS_TABLE in tables
        else []
    )
    version_columns = tuple(str(row[1]) for row in version_schema_rows)
    version_rows: list[Any] = (
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
