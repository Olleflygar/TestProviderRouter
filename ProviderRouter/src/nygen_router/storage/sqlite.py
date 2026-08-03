from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from nygen_router.config import ApiProtocol
from nygen_router.metrics import MetricsEvent
from nygen_router.storage.base import (
    CREATE_PROVIDER_ATTEMPTS_TABLE_SQL,
    INSERT_PROVIDER_ATTEMPT_SQL,
    TABLE_INFO_SQL,
    build_query_recent_sql,
    event_to_params,
    row_to_event,
    validate_provider_attempts_schema,
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
        connection = self._connect()
        connection.execute(INSERT_PROVIDER_ATTEMPT_SQL, event_to_params(event))
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
            if self.path.exists():
                uri = f"file:{quote(str(self.path))}?mode=ro"
                inspection = sqlite3.connect(uri, uri=True)
                try:
                    rows = inspection.execute(TABLE_INFO_SQL).fetchall()
                    if rows:
                        validate_provider_attempts_schema(rows)
                finally:
                    inspection.close()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path))
            connection.execute(CREATE_PROVIDER_ATTEMPTS_TABLE_SQL)
            validate_provider_attempts_schema(connection.execute(TABLE_INFO_SQL).fetchall())
            connection.commit()
            self._connection = connection
        return self._connection
