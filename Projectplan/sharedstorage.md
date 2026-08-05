# Shared Storage Design Context

## Status and purpose

This document records the current design direction for PR13 (storage
versioning/shared-backend foundations), PR25 (durable local health), and PR14
(shared PostgreSQL organizational state). It is context for future planning and
implementation prompts, not a substitute for inspecting the source and tests.

Use sources in this order when they disagree:

1. Current source and tests.
2. [`NewProjectPlan.md`](NewProjectPlan.md).
3. This design-context document.
4. Historical rationale in [`OldProjectPlan.md`](OldProjectPlan.md).

The repository-wide and package-local `AGENTS.md` files remain binding. In
particular, core imports must stay lightweight, storage remains best-effort,
provider responses stay untouched, and tests must use public injection seams
rather than monkeypatching internal collaborators.

## Executive decision

Do **not** introduce a universal, mandatory ORM layer across the current local
storage backends.

Preserve the existing storage-neutral `MetricsStore` protocol and use three
explicit backend paths:

```text
ProviderRouter / ScoreBasedPolicy
              |
              v
       MetricsStore protocol
          |-- DuckDBMetricsStore
          |      `-- native DuckDB driver (local default)
          |
          |-- SQLiteMetricsStore
          |      `-- Python stdlib sqlite3 (minimal local alternative)
          |
          `-- PostgresMetricsStore
                 `-- optional SQL toolkit/driver
                        |-- recommended starting point: SQLAlchemy Core
                        `-- psycopg -> PostgreSQL -> Supabase or another host
```

`PostgresMetricsStore` is the proposed optional global metrics backend. It
targets PostgreSQL, so it works with Supabase and conventional PostgreSQL
deployments without depending on the Supabase Data API or Supabase SDK.

SQLAlchemy Core is the recommended starting implementation for the PostgreSQL
path because it supplies engines, connection pooling, transactions, table
metadata, parameterized expressions, and migration integration without adding
full ORM entity/session semantics. This remains an internal implementation
choice to validate with a focused compatibility spike; it is not part of the
router's public API.

If a second genuinely different client/server SQL engine is later required,
extract the portable SQLAlchemy table/query operations from the PostgreSQL
implementation at that time. Do not build a general multi-engine persistence
framework before there is a second real consumer.

## Terminology

### SQL

SQL is a language used by relational database systems. “SQL database” is a
category, not one implementation. PostgreSQL, MySQL, MariaDB, SQLite, DuckDB,
Oracle, and Microsoft SQL Server are different database engines with different
drivers and operational behavior.

### PostgreSQL / Postgres

Postgres is the common short name for PostgreSQL. PostgreSQL is a specific
client/server relational database system. It is not middleware and is not a
generic name for SQL databases.

### Supabase

Supabase is a managed platform whose database is PostgreSQL. For this design,
the relevant connection is:

```text
PostgresMetricsStore -> PostgreSQL protocol -> PostgreSQL hosted by Supabase
```

Supabase also offers HTTP APIs, authentication, storage, and other services,
but the initial global metrics backend does not require those products.

### DuckDB and SQLite

DuckDB and SQLite are separate embedded database engines. They run in the
application process and normally persist to local files rather than serving
many applications over a network. DuckDB remains the router's local default;
SQLite remains the standard-library option and the recommended current choice
when several local processes share a file.

### SQLAlchemy Core versus the SQLAlchemy ORM

SQLAlchemy Core is a database toolkit: engines, connections, pools,
transactions, table metadata, types, and composable SQL expressions.

The SQLAlchemy ORM builds on Core and adds mapped entity classes, `Session`, an
identity map, change tracking, autoflush, object expiration, relationships,
cascades, and unit-of-work behavior.

Core and ORM ship in the same `sqlalchemy` package. Choosing Core reduces
semantic and implementation complexity; it does not reduce the installed size
of that package.

## Current repository facts

### Public storage boundary

[`MetricsStore`](../ProviderRouter/src/nygen_router/storage/base.py) is a
structural `typing.Protocol` with exactly two storage operations:

```python
class MetricsStore(Protocol):
    def record_attempt(self, event: MetricsEvent) -> None: ...

    def query_recent(
        self,
        *,
        since: datetime,
        metrics_scope: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        protocol: ApiProtocol | None = None,
        call_type: CallType | None = None,
    ) -> list[MetricsEvent]: ...
```

This is already the correct database abstraction. The router and routing
policies do not need an ORM type, SQLAlchemy engine, connection, session, or
raw row.

Applications select one store when constructing a router. The alternatives are
choices, not a pipeline:

```python
# Default local DuckDB
router = ProviderRouter(providers=providers, metrics_scope="production")

# Explicit native SQLite
router = ProviderRouter(
    providers=providers,
    metrics_scope="production",
    metrics_store=SQLiteMetricsStore("metrics.sqlite"),
)

# Proposed optional global PostgreSQL/Supabase
router = ProviderRouter(
    providers=providers,
    metrics_scope="production",
    metrics_store=PostgresMetricsStore(database_url),
)

# Disable metrics
router = ProviderRouter(
    providers=providers,
    metrics_scope="production",
    metrics_store=None,
)
```

One router does not silently switch stores, dual-write, mirror, or move history
between them.

### Current event model

[`MetricsEvent`](../ProviderRouter/src/nygen_router/metrics.py) is an immutable,
storage-neutral dataclass. It is the domain record; database row types are
private representations.

| Field | Logical meaning | Current routing relevance |
|---|---|---|
| `id` | Unique attempt-event ID | Identity only |
| `timestamp` | Timezone-aware UTC event time | Lookback filtering and optional recency decay |
| `metrics_scope` | Application/workload partition | Query filtering |
| `provider_id` | Canonical provider identity | Provider matching and filtering |
| `provider_name` | Display metadata | Reporting only |
| `model` | Provider model | History partitioning |
| `protocol` | Provider API protocol | History partitioning |
| `call_type` | Regular or streaming | Selects separate statistics/scoring bucket |
| `success` | Attempt outcome | Success-rate scoring |
| `stream_opened` | Whether a streaming connection opened | Stored observation; not scored |
| `latency_ms` | Regular response latency or streaming TTFT | Speed scoring for successful attempts |
| `total_duration_ms` | Total streaming attempt duration | Stored; not currently scored |
| `error_type` | Normalized error category | Diagnostic error/rate-limit/timeout tallies |

There is currently no token usage, cost, response body, request body, sticky
affinity, or durable health state in the metrics table. Health remains separate
in-memory state until PR25 deliberately introduces its own storage-neutral
interface.

### Current routing use of history

`ScoreBasedPolicy` calls `query_recent` during provider ordering, aggregates
the returned `MetricsEvent` objects in Python, and scores only success and
speed. It deliberately falls back to its tie-break policy when metrics are
disabled or history cannot be read.

The router records each provider attempt synchronously. Storage exceptions are
caught so they never replace a provider response, but a remote store can still
delay the call until its timeout expires. A global backend therefore adds
network latency on both sides of a regular score-based invocation:

```text
query global history -> choose provider -> provider call -> write event globally
```

This hot-path cost is a larger transparency concern than Core-versus-ORM.

## Goals

- Preserve a lightweight local default and a standard-library local option.
- Add opt-in shared history for applications across machines/processes.
- Support Supabase through standard PostgreSQL rather than a vendor-specific
  public API.
- Keep `MetricsStore` and `MetricsEvent` storage-neutral.
- Keep all database engines, drivers, pools, connections, sessions, and rows
  private to storage implementations.
- Make backend dependencies optional and lazy.
- Give every bundled backend identical logical event/query behavior.
- Add explicit schema versions and deliberate deployment migrations.
- Bound remote failures and preserve safe routing degradation.
- Keep an easy “bring your own `MetricsStore`” extension path.

## Non-goals

- Supporting every SQL database in the initial shared-storage PR.
- Replacing native DuckDB or SQLite with an ORM implementation.
- Requiring SQLAlchemy, psycopg, Alembic, or Supabase packages for local users.
- Automatically copying DuckDB/SQLite history into PostgreSQL.
- Automatic backend switching, replication, or dual writes.
- Automatically altering a managed production schema during router startup.
- Adding caching, background queues, batching, rollups, or retention without
  separately specified freshness/durability semantics.
- Moving aggregation or score calculation into an ORM entity model.
- Persisting health as an incidental part of the metrics-backend PR; PR25 owns
  the health-state interface and local implementation.

## Why a universal ORM is not the best current fit

### The reusable database behavior is tiny

The metrics database has one append-only event table and two operations:

```text
INSERT one attempt
SELECT recent attempts with optional equality filters
```

There are no relationships, joins, cascading operations, mutable entity
graphs, or coordinated CRUD workflows. The principal reusable code is a table
description, one insert, one select, and row conversion. That does not justify
imposing a general ORM architecture on all local users and backends.

### The project already has the correct abstraction

`MetricsStore` expresses behavior at the router boundary. It permits SQL,
NoSQL, HTTP, in-memory, or proprietary implementations without leaking their
technology into routing. Replacing this with a public ORM abstraction would be
less general, not more general.

### A full ORM duplicates the domain model

`MetricsEvent` already represents an attempt. A full ORM would either:

1. Couple `MetricsEvent` to SQLAlchemy mapping and weaken storage neutrality;
   or
2. Add a nearly identical private `ProviderAttemptRow` mapped class plus
   conversions between it and `MetricsEvent`.

The second option is viable but adds little value when events are immutable and
never updated as managed objects.

### ORM session semantics do not serve this workload

Identity maps, object tracking, autoflush, expiration, relationship loading,
cascades, and unit-of-work behavior solve problems the metrics store does not
have. They also introduce additional lifecycle and transaction behavior that a
future maintainer must understand and test.

### A universal ORM would weaken lightweight storage

Today, SQLite needs no package outside the Python standard library and DuckDB
is an optional, lazily imported dependency. Routing can also run with metrics
disabled.

Forcing every store through a universal ORM could make SQLAlchemy mandatory,
add `duckdb-engine` to the default DuckDB path, remove SQLite's standard-library
property, and make root imports more fragile. Users who never need shared
storage would pay for abstractions chosen for the remote backend.

### An ORM does not erase database differences

Every new engine still needs:

- A database driver and dialect.
- Connection, pooling, TLS, and timeout configuration.
- Physical type decisions.
- Migration support.
- Transaction and concurrency validation.
- Real-engine integration tests.
- Operational documentation.

An ORM compiles SQL; it does not make embedded DuckDB, file-backed SQLite,
networked PostgreSQL, and MySQL operationally equivalent.

### An ORM does not provide migrations automatically

Schema history must still be versioned, reviewed, deployed, and tested.
Alembic can consume SQLAlchemy Core `MetaData`; full ORM mapping is not required.

### An ORM does not solve global-history scale or latency

The current score policy queries raw recent events on every call and aggregates
them in Python. A remote ORM still performs a network query and transfers those
events. Each recorded attempt is another network transaction. Pooling helps
connection setup but does not eliminate network latency or unbounded history.

### The portability advantage is modest for one remote engine

Switching among Supabase, self-hosted PostgreSQL, and other conventional
PostgreSQL hosting providers requires only a different connection URL under
either design. A universal ORM becomes materially more valuable only when the
project commits to multiple distinct SQL engines.

If MySQL later becomes the second engine, portable Core operations can be
extracted once. Subsequent SQL engines then receive nearly the same reuse as an
up-front general ORM, without paying the abstraction cost before it is needed.

## Chosen architecture in detail

### Native local backends

Keep `DuckDBMetricsStore` and `SQLiteMetricsStore` native and public. Preserve
their current constructors, best-effort router behavior, exact logical event
contract, and conformance tests.

- DuckDB remains the local default and retains its current single-process
  limitation.
- SQLite retains its standard-library-only guarantee and current cross-process
  local role.
- A missing local table may be created on first use.
- An incompatible local file is never silently deleted, rewritten, or altered.

### Optional `PostgresMetricsStore`

Add a public store implementing the existing protocol:

```python
class PostgresMetricsStore:
    def __init__(self, database_url: str, ...) -> None: ...
    def record_attempt(self, event: MetricsEvent) -> None: ...
    def query_recent(self, *, since: datetime, ...) -> list[MetricsEvent]: ...
    def close(self) -> None: ...
```

The exact constructor options must be specified by the implementation prompt,
but the store must own or clearly accept:

- A standard PostgreSQL URL.
- Direct versus externally pooled connection mode.
- Connect and statement timeouts.
- TLS/SSL settings.
- Safe pool sizing and cleanup.
- Password-safe URL and parameter rendering.

`close()` should remain idempotent like the current bundled stores. The
`MetricsStore` protocol does not currently require `close`, and the router
should not unexpectedly close a store injected and owned by the application.

### Recommended PostgreSQL internal flow

```text
MetricsEvent
    -> event_to_record(event): mapping of logical column names to values
    -> SQLAlchemy Core INSERT
    -> PostgreSQL dialect compilation
    -> psycopg bound parameters
    -> PostgreSQL server (for example Supabase)
```

The read path reverses this:

```text
PostgreSQL rows
    -> SQLAlchemy RowMapping
    -> record_to_event(row)
    -> list[MetricsEvent]
    -> existing Python aggregation and scoring
```

The PostgreSQL store should use short-lived connections/transactions obtained
from one process-level engine owned by the store. It should not keep one ORM
`Session` or ORM entity graph alive across calls.

The store should not swallow driver/toolkit errors. Direct store callers need
the real error; the existing router and score-policy boundaries own graceful
degradation and warning deduplication.

### Shared conversion before shared SQL

The first reusable extraction should be database-neutral serialization, not a
general repository hierarchy:

```python
event_to_record(event: MetricsEvent) -> dict[str, object]
record_to_event(record: Mapping[str, object]) -> MetricsEvent
```

Native stores can derive positional parameters from the named record.
SQLAlchemy can bind the mapping directly. This gives every backend one source
for enum values, null handling, booleans, timestamps, and field names while
leaving connection/query code appropriately backend-specific.

With only one SQLAlchemy-backed store, keep table metadata and insert/select
logic in `postgres.py` or one small private module. Extract `_sql/schema.py`
and `_sql/repository.py` only when a second backend actually reuses them.

## Logical and physical schema

### Logical schema contract

All stores must preserve the `MetricsEvent` fields and `query_recent` behavior.
They need the same logical schema, not byte-for-byte identical physical DDL.

The following PostgreSQL DDL illustrates the intended first remote schema; the
implementation prompt must reconcile exact names/types with the current source
and tests before shipping:

```sql
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
);

CREATE INDEX provider_attempts_scope_timestamp_idx
    ON provider_attempts (metrics_scope, timestamp);

CREATE INDEX provider_attempts_timestamp_idx
    ON provider_attempts (timestamp);
```

Do not initially use PostgreSQL-specific enum types. Storing `protocol`,
`call_type`, and `error_type` as strings makes enum evolution and later SQL
engine support simpler.

The first indexes match the dominant queries:

- Default history scope: `metrics_scope` plus a timestamp lower bound.
- All-scope history: timestamp lower bound.

Do not add an index for every possible filter without measured need; indexes
increase write cost and storage.

### Cross-engine physical differences

| Logical value | PostgreSQL recommendation | Current local representation | MySQL/MariaDB concern |
|---|---|---|---|
| Event ID | `TEXT PRIMARY KEY` | `TEXT PRIMARY KEY` | Indexed `TEXT` has restrictions; likely use a bounded `VARCHAR` after defining the ID-length contract |
| UTC timestamp | `TIMESTAMPTZ` | ISO-8601 text | `DATETIME(6)` does not retain an offset; normalize to UTC and restore UTC on read |
| Boolean | `BOOLEAN` | SQLite/DuckDB logical integer handling | MySQL `BOOLEAN` maps to a small integer; test exact round-trip behavior |
| Latency | `DOUBLE PRECISION` | `REAL`/logical float | Use an appropriate double-precision type |
| Enum-like values | `TEXT` | `TEXT` | Prefer strings over engine-owned enum types |
| Nullable observations | Nullable columns | Nullable columns | Verify driver `NULL` conversion |

The contract is behavioral: field-for-field round trips, UTC timestamps,
filtering, ordering, and identity partitioning. Physical type spelling may
differ by engine.

## Schema versioning and migrations

### Schema migration versus data migration

These are different features:

```text
Schema migration:
    PostgreSQL provider_attempts v1 -> PostgreSQL provider_attempts v2

Cross-backend data migration:
    DuckDB history -> PostgreSQL history
```

PR13 needs the first. Automatic cross-backend copying is a non-goal.

### Local schema expectations

For embedded stores:

- Create a missing current schema on first use.
- Inspect an existing schema before writing.
- Never silently check-and-alter, delete, replace, or backfill an incompatible
  user database.
- Any future local migration must be an explicit versioned operation.

### Managed PostgreSQL expectations

For PostgreSQL/Supabase:

- Provision and upgrade the schema through an explicit deployment/admin step.
- Do not grant normal router runtime connections schema-owner privileges.
- Do not automatically run production migrations from `ProviderRouter`
  construction or normal calls.
- Verify the expected schema revision at a deliberate boundary (normally first
  use or explicit validation), then fail with an actionable mismatch error.
- Use a least-privilege runtime role with only the operations the selected
  state stores require.

Migration tooling may be an optional extra. If Alembic is selected, its version
history and SQLAlchemy metadata stay private to the storage package. Users who
use only native DuckDB or SQLite do not install Alembic.

The schema definition used by runtime queries and the latest migration must be
kept consistent. Add a test that provisions a fresh PostgreSQL database through
the migration path and then runs the normal conformance suite.

## Dependencies and import boundaries

The current core package has a deliberately small dependency surface. Preserve
that shape with extras similar to:

```toml
[project]
dependencies = [
    "pydantic>=2,<3",
]

[project.optional-dependencies]
duckdb = [
    "duckdb",
]

postgres = [
    "sqlalchemy",
    "psycopg[binary]",
]

postgres-migrations = [
    "sqlalchemy",
    "psycopg[binary]",
    "alembic",
]
```

Exact versions belong in the implementation prompt after testing the supported
matrix.

Required import behavior:

- `import nygen_router` must not require SQLAlchemy or psycopg.
- Using local native stores must not import PostgreSQL dependencies.
- Merely exporting `PostgresMetricsStore` from the package root must not make
  optional imports mandatory at module import time.
- Selecting `PostgresMetricsStore` without its extra must produce a clear,
  credential-safe installation error.
- Network connections should remain lazy rather than occurring during root
  package import.

## Supabase operation

Supabase is one PostgreSQL deployment target, not its own store abstraction.
`PostgresMetricsStore` accepts the PostgreSQL connection information Supabase
provides.

The implementation must document at least:

- Direct connections for suitable long-lived backends.
- Session or transaction pooler use when required by network/deployment shape.
- The interaction between SQLAlchemy pooling and an external pooler.
- Prepared-statement restrictions in transaction-pooling modes.
- SSL certificate/verification configuration.
- Separate migration and runtime roles/connection strings.
- Connection, statement, and pool timeouts.

Do not select a pool mode silently. Expose a small explicit configuration or
accept documented SQLAlchemy engine/connect options without leaking the engine
through core APIs.

## Adding another shared SQL database

### Another PostgreSQL hosting provider

Supabase, self-hosted PostgreSQL, and conventional managed PostgreSQL services
all use `PostgresMetricsStore`. Changing hosts requires connection/deployment
configuration, not a new store implementation or ORM layer.

### A genuinely different engine: MySQL or MariaDB

“MariaSQL” is not the usual product name; the common database is MariaDB.
MySQL and MariaDB are related engines but still require their own driver,
dialect validation, migration path, and tests.

When the first non-PostgreSQL client/server engine is approved:

1. Add an explicit optional driver extra, for example a future `mysql` extra.
2. Add an explicit store such as `MySQLMetricsStore`; do not make the public API
   guess a backend from an arbitrary URL.
3. Extract the portable SQLAlchemy Core `Table`, insert, select, and filter
   logic from `PostgresMetricsStore` into a small private shared module.
4. Keep engine construction, connection arguments, timeout handling, and
   dialect exceptions in the concrete backend wrapper.
5. Define a physical MySQL/MariaDB schema satisfying the logical event
   contract. Resolve text-primary-key length, UTC timestamp, boolean, collation,
   and index-length differences explicitly.
6. Add a separately versioned migration configuration or verified dialect path.
7. Run the complete conformance suite and real MySQL/MariaDB integration tests.
8. Document concurrency, pooling, TLS, permissions, supported versions, and
   known limitations.

At that point the private layout may justifiably become:

```text
storage/
|-- base.py
|-- duckdb.py
|-- sqlite.py
|-- postgres.py
|-- mysql.py
`-- _sql/
    |-- schema.py
    `-- repository.py
```

This extraction gives subsequent SQL engines most of the portability benefit
of an up-front universal ORM, while preserving simplicity until there is real
reuse.

### Other custom storage systems

Users may implement `MetricsStore` directly for a non-SQL database or service.
They do not need SQLAlchemy or the PostgreSQL schema. They must provide the same
logical behavior:

- Record one event per call to `record_attempt`.
- Preserve every `MetricsEvent` field.
- Reject naive `since` timestamps consistently.
- Apply every requested filter.
- Return chronological ascending results.
- Preserve canonical identity partitioning.
- Raise real storage errors so router-owned degradation remains observable.

## Testing requirements

Every bundled backend must run the shared
[`test_metrics_store.py`](../ProviderRouter/tests/test_metrics_store.py)
conformance suite. Extend it rather than replacing existing regression tests.

A remote backend also needs coverage for:

- Fresh schema provisioned through the supported migration path.
- Exact field round trips and UTC handling.
- Combined query filters and chronological ordering.
- Concurrent inserts from independent connections/processes.
- Transaction rollback after failed inserts.
- Connection refusal, connect timeout, and statement timeout.
- Pool exhaustion/recovery behavior where configurable.
- Idempotent cleanup.
- Schema revision mismatch without automatic alteration.
- Password/parameter redaction in errors and logs.
- Router write degradation and score-policy read degradation.
- Real PostgreSQL in CI without live production credentials.

Do not monkeypatch internal SQLAlchemy/driver collaborators. Test the store
through its public constructor against a real test database, and test router
degradation through the existing injected `MetricsStore` seam.

## User-facing storage expectations

The router should offer these support levels:

| Level | Expected experience |
|---|---|
| Local default | Native DuckDB, zero configuration after installing its existing optional extra |
| Minimal local | Native SQLite using Python's standard library |
| Global organizational | Optional `PostgresMetricsStore`, including Supabase, with explicit deployment migrations |
| Custom | Any user implementation satisfying `MetricsStore` |

Users should be able to expect:

- One explicit backend per router.
- No hidden dependency installation or imports.
- No hidden backend switching or data copying.
- Stable `MetricsEvent` and query semantics.
- Backend-specific setup and compatibility documentation.
- Safe, bounded degradation when storage is unavailable.
- No database error replacing a successful provider result.
- No credentials or sensitive bound values in logs.
- A conformance suite for custom implementations.

Switching from a local store to PostgreSQL begins using the history present in
PostgreSQL. It does not automatically import the local file. With empty or
unavailable global history, score-based routing uses its documented optimistic
start and tie-break behavior.

## Performance and transparency risks

### Remote reads on provider selection

The current score policy fetches recent raw events on every call. With global
history, a large scope can create significant network transfer and Python
aggregation work. Initial mitigation:

- Index `(metrics_scope, timestamp)` and `timestamp`.
- Use bounded connection and statement timeouts.
- Reuse connections appropriately.
- Preserve tie-break fallback on query failure.
- Measure returned row counts and query latency.

Do not silently add caching because it changes history freshness. If scale
requires server-side aggregates, rollups, or caching, specify those semantics
in a later PR and evolve the storage interface deliberately.

### Remote writes before returning

Regular attempt recording is synchronous today. A remote write adds latency
after the provider responds but before the caller receives that response.
Connection pooling reduces setup cost but not network transit.

Initial behavior should remain synchronous and semantically consistent, with
strict timeouts and safe degradation. Background writes or batching may improve
latency but introduce event-loss, flush, shutdown, retry, ordering, and
backpressure semantics; they require a separate explicit design.

### Reporting versus routing source of truth

Before implementation, confirm whether Supabase is intended to be:

1. The live source for score-based routing history; or
2. A central reporting/analytics destination while routing remains local.

The current `MetricsStore` combines writes and score-history reads. The second
use case would require a deliberate composite store or separate telemetry sink;
do not smuggle dual-write behavior into `PostgresMetricsStore`.

## When to reconsider a full ORM

A full ORM becomes more compelling if the persisted domain later contains
several related mutable entities requiring coordinated transactions, for
example organizations, users, provider registrations, routing profiles,
durable health state, metrics, and reporting objects with real relationships.

Reconsider when one or more of these becomes true:

- Several related tables form a genuine object graph.
- Relationships, cascades, or coordinated updates are required.
- Multiple distinct SQL engines are committed near-term requirements.
- Mapped entities remove more conversion/transaction code than they add.
- The package accepts the dependency and semantic cost for all affected users.

Until then, one immutable event table and two operations do not justify a
universal ORM.

## Suggested PR boundaries

### PR13: storage versioning/shared-backend foundation

- Pin the logical storage contract and dependency/import boundaries.
- Introduce explicit schema-version and migration conventions.
- Define local versus managed schema ownership.
- Refactor shared named event conversion if useful.
- Add remote connection/error/timeout expectations.
- Do not need to ship PostgreSQL merely to create a general ORM framework.

### PR25: durable local health

- Define a separate storage-neutral health-state interface.
- Implement the local DuckDB health backend.
- Preserve safe fallback to in-memory health.
- Do not overload `MetricsStore` with health semantics.

### PR14: PostgreSQL organizational state

- Add optional `PostgresMetricsStore` for metrics.
- Add the PostgreSQL health backend against PR25's interface.
- Supply explicit PostgreSQL/Supabase migrations and deployment guidance.
- Add real PostgreSQL conformance/integration coverage.
- Preserve DuckDB as the local default and SQLite as the native minimal option.

## Open decisions for the implementation prompt

The architectural direction is established, but a later agent must still
resolve these details from current source, tests, and measured behavior:

1. Whether the first `PostgresMetricsStore` uses SQLAlchemy Core or direct
   psycopg; Core is recommended, but the compatibility/dependency spike should
   justify it concretely.
2. Exact dependency version ranges and supported PostgreSQL versions.
3. The public constructor's timeout, pooling, TLS, and advanced-engine seams.
4. The schema-version mechanism and migration command/deployment interface.
5. Exact PostgreSQL physical types and whether timestamp representation changes
   from the local ISO-text representation.
6. How schema validation is triggered without adding an unbounded network call
   to every router invocation.
7. Whether Supabase is the live scoring source or only a reporting destination.
8. Acceptable remote read/write latency and timeout budgets.
9. Data-retention expectations for an append-only global event table.
10. The event-volume threshold that would require server-side aggregation,
    rollups, or caching.
11. Whether PostgreSQL metrics and health migrations share one revision stream
    after PR25/PR14 or remain explicitly separated.

## External references

- [SQLAlchemy Core and ORM architecture](https://docs.sqlalchemy.org/en/20/intro.html)
- [SQLAlchemy supported and external dialects](https://docs.sqlalchemy.org/en/20/dialects/)
- [Alembic migration documentation](https://alembic.sqlalchemy.org/en/latest/)
- [Supabase PostgreSQL connection modes](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [`duckdb-engine` SQLAlchemy dialect notes](https://pypi.org/project/duckdb-engine/)

