from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from nygen_router.metrics import MetricsEvent
from nygen_router.storage.base import (
    CREATE_PROVIDER_ATTEMPTS_TABLE_SQL,
    INSERT_PROVIDER_ATTEMPT_SQL,
    TABLE_INFO_SQL,
    build_query_recent_sql,
    event_to_params,
    existing_column_names,
    missing_column_sql,
    row_to_event,
)


class SQLiteMetricsStore:
    """Stdlib sqlite3-backed MetricsStore -- no optional dependency required.

    The recommended option when several local processes must share one store:
    SQLite handles cross-process file locking natively, unlike DuckDB.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    def record_attempt(self, event: MetricsEvent) -> None:
        connection = self._connect()
        connection.execute(INSERT_PROVIDER_ATTEMPT_SQL, event_to_params(event))
        connection.commit()

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
        rows = connection.execute(query, params).fetchall()
        return [row_to_event(row) for row in rows]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path))
            connection.execute(CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
            columns = existing_column_names(connection.execute(TABLE_INFO_SQL).fetchall())
            for statement in missing_column_sql(columns):
                connection.execute(statement)
            connection.commit()
            self._connection = connection
        return self._connection
