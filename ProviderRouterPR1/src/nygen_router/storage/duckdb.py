from __future__ import annotations

import importlib.util
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nygen_router.metrics import MetricsEvent
from nygen_router.storage.base import (
    CREATE_PROVIDER_ATTEMPTS_TABLE_SQL,
    INSERT_PROVIDER_ATTEMPT_SQL,
    build_query_recent_sql,
    event_to_params,
    row_to_event,
)

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
        connection = self._connect()
        connection.execute(INSERT_PROVIDER_ATTEMPT_SQL, list(event_to_params(event)))

    def query_recent(
        self,
        *,
        since: datetime,
        provider_name: str | None = None,
        model: str | None = None,
    ) -> list[MetricsEvent]:
        query, params = build_query_recent_sql(
            since=since, provider_name=provider_name, model=model
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

            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = duckdb.connect(str(self.path))
            connection.execute(CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
            self._connection = connection
        return self._connection
