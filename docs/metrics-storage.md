# Metrics & storage

Every provider attempt (success or failure) is recorded as one scoped
`MetricsEvent`. Excluded providers are not recorded. Scoring identity is
`metrics_scope + provider_id + model + protocol + call_type`.

Writes are best-effort: a storage failure never replaces a successful provider
response. Pass `metrics_store=None` to turn persistence off entirely.

## Choosing a store

```python
from llm_provider_router import (
    DuckDBMetricsStore,
    PostgresMetricsStore,
    ProviderRouter,
    SQLiteMetricsStore,
)

# Default (omit metrics_store): DuckDB at ~/.nygen_router/metrics.duckdb
router = ProviderRouter(providers=[...], metrics_scope="my-app")

# Shared local file across processes
router = ProviderRouter(
    ...,
    metrics_store=SQLiteMetricsStore("metrics.sqlite"),
)

# Shared organizational Postgres (live scoring source)
router = ProviderRouter(
    ...,
    metrics_store=PostgresMetricsStore(
        "postgresql://app_user:...@db.example.com:5432/routing",
        config={"pool_mode": "direct"},
    ),
)
```

| Backend | Extra | Good for |
|---------|-------|----------|
| DuckDB (default) | `[duckdb]` | Single-process local history |
| SQLite | none (stdlib) | Several local processes, one file |
| Postgres | `[postgres]` | Multi-machine / org-wide scoring |

**Do not** point multiple processes at one DuckDB file. Use SQLite or Postgres
instead. See [Concurrency](./concurrency.md).

Separate materially different workloads with different `metrics_scope` values —
the router does not bucket by prompt size.

## What gets recorded

- Regular `latency_ms`: full-response latency on success.
- Streaming `latency_ms`: time to first chunk (TTFT); `NULL` if no chunk arrived.
- `total_duration_ms`: whole attempt, including pre-open failure for streams.
- `stream_opened`: `None` / `False` / `True` depending on whether a normalized
  stream was returned.

Failed attempts never enter latency averages. Regular and streaming history
are never blended when scoring.

## Schema administration (CLI)

Runtime stores never create or migrate an *existing* database. Fresh DuckDB /
SQLite files at an **absent** path can be created at metrics version 2; Postgres
schema is **only** created by an explicit admin act.

```sh
# Local
llm-provider-router storage inspect --backend duckdb --default
llm-provider-router storage create --backend sqlite --path ./metrics.sqlite

# Postgres — prefer the env var so the password stays out of shell history
export NYGEN_ROUTER_POSTGRES_URL='postgresql://owner:...@db.example.com:5432/routing'
llm-provider-router storage create --backend postgres
llm-provider-router storage inspect --backend postgres
```

Same operations exist as typed Python (`inspect_database`, `create_database`,
`migrate_database`). Today there is no v1→v2 migration route: archive or delete
an old target while stopped, or point at a new absent path.

Creating a non-default file does not rewire a running router — pass that path
explicitly as `metrics_store=...`.

### Postgres notes

- Speaks standard PostgreSQL via `psycopg` (works with Supabase as a host).
  Never uses the Supabase Data API.
- Runtime role needs `INSERT`/`SELECT` on `provider_attempts` and `SELECT` on
  `nygen_router_schema_versions`.
- Set `pool_mode` to match your connection (`direct`, `session_pooler`,
  `transaction_pooler`). Encryption defaults to required.
- Selecting Postgres starts from whatever is already in that database — no
  import or dual-write from DuckDB/SQLite.

Timeouts default latency-first so a slow DB does not stall routing for long;
the router holds its lock across storage calls, so those bounds matter under
load.

## Custom backends

`MetricsStore` is a three-method protocol: `record_attempt`, `query_recent`, and
`query_score_aggregates`. Implement all three and pass the instance as
`metrics_store=`. Score-based routing always uses the aggregate call — it never
falls back to raw history.

For conformance, see `tests/test_metrics_store.py` and
`tests/test_pr30_storage_source.py` in the package.

## Score-based routing

How history becomes attempt order lives in [Policies](./policies.md). Deep
schema and aggregation detail remains in the [package README](../ProviderRouter/README.md).
