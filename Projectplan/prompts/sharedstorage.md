# Shared Storage Design Context

## Status and purpose

This document records the shipped PR13 local storage foundation, shipped PR30
storage-side score aggregation, and the current design direction for PR25
(durable local health) and PR14 (shared PostgreSQL organizational state). It is
context for future planning and implementation prompts, not a substitute for
inspecting the source and tests.

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
structural `typing.Protocol` with exactly three mandatory storage operations:

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

    def query_score_aggregates(
        self,
        query: ScoreAggregateQuery,
    ) -> list[ScoreAggregate]: ...
```

This is the shipped PR30 contract, not an optional capability. Router
construction rejects path strings, legacy two-method implementations, and
missing/non-callable methods before provider contact. `query_recent` remains
the raw chronological event operation for direct callers, diagnosis,
conformance, and Python-reference comparisons; `ScoreBasedPolicy` always uses
`query_score_aggregates` and never falls back to raw history. The router and
routing policies do not need an ORM type, SQLAlchemy engine, connection,
session, expression, or raw row.

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

### Shipped local schema administration (PR13)

DuckDB and SQLite now share one named event conversion and one authoritative
logical schema definition. A fresh absent path receives `provider_attempts` and
the component-specific metadata table:

```text
nygen_router_schema_versions
component TEXT PRIMARY KEY
version   INTEGER NOT NULL
```

Fresh databases now record `metrics = 2`; future health storage uses an
independent component revision. Normal runtime creates this schema only when
its configured file path is absent. A current database is validated and reused
without DDL. The exact unversioned PR29 table is still recognized as an
implicit version-1 baseline, but PR30 runtime rejects it unchanged, as it does
an explicitly versioned v1 target. PR30 provides no v1-to-v2 migration:
administrators must stop writers and manually archive/delete a disposable old
target or configure an absent path.

Metrics v2 includes backend-specific measured index requirements. SQLite
requires exactly one
`(provider_id, model, protocol, call_type, timestamp)` index; its current- and
all-scope query plans both use it. DuckDB requires no score-query index because
its analytical plan is a sequential scan with or without the tested ART
indexes. Missing or malformed required project-owned indexes make a claimed v2
database incompatible without changing it.

Administration is deliberately separate from `MetricsStore`:

```text
inspect_database(LocalBackend, path)  # strictly read-only
create_database(LocalBackend, path)   # absent targets only
migrate_database(LocalBackend, path)  # explicit offline known routes only
```

The same implementation is available through `nygen-router storage
inspect|create|migrate`. Creation never overwrites. Migration takes an
exclusive offline transaction, validates the full route before writing,
updates versions in the same transaction after each real step, and can create
one explicitly named, engine-safe, validated pre-migration backup. There is
currently no executable migration step: the PR13 implicit-v1 stamping route was
superseded by PR30's fresh-only v2 decision. Unknown historical layouts and
v1/implicit-v1 targets are not guessed, transformed, stamped, or reindexed.

### Current routing use of history

`ScoreBasedPolicy` applies its tie-break policy once, captures one reference
time, and makes exactly one `query_score_aggregates` call per non-empty,
metrics-enabled ordering. DuckDB and SQLite each execute one parameterized
aggregate query for every requested provider at once; there is no
per-provider query or raw-history fallback.

The aggregate query matches the lower time bound, optional exact metrics scope,
provider ID, model, protocol, and caller-declared call type. It returns bounded
intermediate totals: weighted attempts and successes, successful
non-NULL-latency weight and weighted total, plus exact unweighted
error/rate-limit/timeout tallies. Shared Python derives success rates and
latency/TTFT averages, constructs `ProviderStats` from the current provider
configuration, and calculates the final score. SQL does not own the final
score.

Flat mode weights every event 1.0 over `lookback_hours`. Exponential mode
queries exactly six half-lives and computes
`0.5 ** (age_hours / half_life_hours)` from the same policy-captured reference
time for every row. An explicitly returned all-zero provider has genuine empty
history and receives the optimistic-start score, then self-corrects as attempts
arrive. A missing, duplicate, unexpected, or malformed row invalidates the
whole read; it is never converted to optimistic evidence. Aggregate exception
or invalid data returns the exact baseline order (round robin by default) and
does not prevent provider invocation.

The router records each provider attempt synchronously. Storage exceptions are
caught so they never replace a provider response, but a remote store can still
delay the call until its timeout expires. A global backend therefore adds
network latency on both sides of a regular score-based invocation:

```text
query global history -> choose provider -> provider call -> write event globally
```

This hot-path cost is a larger transparency concern than Core-versus-ORM.

### Measured PR30 local evidence

Run from the package root:

```sh
.venv/bin/python benchmarks/pr30_score_aggregation.py --rows 60000 --repetitions 7
```

The benchmark generated 60,000 logical events, requested 9 provider
partitions, returned exactly 9 rows per query, and timed 7 repetitions after a
warm-up. SQLite's retained single index produced medians of 1.240208 ms for the
current-scope query and 2.090667 ms for all scopes, compared with
43.191167/43.610417 ms unindexed. A second scope-leading index reduced the
current-scope median to 0.556125 ms but was rejected because one index already
served both plans and the second increased write/storage cost.

DuckDB used sequential scans with no index and with two candidate ART indexes.
No-index medians were 8.648875/9.006959 ms; two-index medians were
8.202791/9.003167 ms, with higher seed/storage cost. Therefore no decorative
DuckDB score-query index is part of v2. These figures are one-machine evidence,
not a universal latency guarantee; bounded result cardinality and captured
plans are the durable acceptance criteria.

The authorized demo reset discarded 2 rows from the default
`~/.nygen_router/metrics.duckdb` and 46 rows from
`WorkflowTests/workflow_history.duckdb`, recreated both empty at metrics v2
after smoke validation, and left
`WorkflowTests/workflow_history.pre-pr29.duckdb` untouched. This was a one-time
repository setup action, never runtime overwrite behavior.

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

The metrics database has one append-only event table and three runtime
operations:

```text
INSERT one attempt
SELECT recent attempts with optional equality filters
SELECT bounded scoring totals for requested provider partitions
```

There are no persisted entity relationships, cascading operations, mutable
entity graphs, or coordinated CRUD workflows. PR30's aggregate query does use a
backend-private requested-provider relation and left join, but that still does
not justify imposing a general ORM architecture on all local users and
backends.

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

### An ORM does not solve global-history latency

PR30 bounded the score-policy result to one aggregate row per requested
provider, but a remote implementation still performs a network query on
selection and another transaction for each synchronously recorded attempt.
Pooling helps connection setup but does not eliminate network transit,
database execution time, or the write hot path.

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
- An absent configured local database path may be created on first use.
- An incompatible local file is never silently deleted, rewritten, or altered.

### Optional `PostgresMetricsStore`

Add a public store implementing the existing protocol:

```python
class PostgresMetricsStore:
    def __init__(self, database_url: str, ...) -> None: ...
    def record_attempt(self, event: MetricsEvent) -> None: ...
    def query_recent(self, *, since: datetime, ...) -> list[MetricsEvent]: ...
    def query_score_aggregates(
        self, query: ScoreAggregateQuery
    ) -> list[ScoreAggregate]: ...
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
ScoreAggregateQuery
    -> one parameterized PostgreSQL aggregate query
    -> bounded SQLAlchemy RowMapping values
    -> list[ScoreAggregate]
    -> shared Python ProviderStats and final score
```

Raw `query_recent` separately continues to convert PostgreSQL rows through
`record_to_event` for direct callers and conformance. It is never the
`ScoreBasedPolicy` fallback.

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

-- PR14 must add only PostgreSQL indexes justified by its own measured plans.
```

Do not initially use PostgreSQL-specific enum types. Storing `protocol`,
`call_type`, and `error_type` as strings makes enum evolution and later SQL
engine support simpler.

PR30's SQLite index is evidence for the logical filter shape, not a PostgreSQL
index prescription. PR14 must benchmark exact current- and all-scope aggregate
plans against realistic PostgreSQL data, retain the smallest useful index set,
and validate those project-owned definitions in its schema. Do not add an index
for every possible filter; indexes increase write cost and storage.

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

Shipped PR13 establishes the first for local backends. Automatic cross-backend
copying remains a non-goal.

### Local schema expectations

For embedded stores:

- Create a current versioned schema only when the configured file path is absent.
- Inspect an existing schema before writing.
- Never silently check-and-alter, delete, replace, or backfill an incompatible
  user database.
- Recognize versioned v1 and the exact unversioned PR29 implicit-v1 baseline
  read-only, but reject both unchanged because PR30 has no v1-to-v2 route.
- Run every future local migration as an explicit offline, consecutive,
  transactional administration operation.

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
- Implement one `query_score_aggregates` operation that returns exactly one
  validated `ScoreAggregate` per distinct requested provider, including
  explicit zeros for genuine empty history.
- Match scope (unless all-scope), provider ID, model, protocol, call type, and
  lower time bound; implement flat and exponential weighting from the supplied
  one reference time.
- Return intermediate totals only. Keep `ProviderStats` and final scoring in
  shared Python, and never substitute raw-history fallback.
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
- Stable raw `MetricsEvent` semantics plus mandatory bounded aggregate
  cardinality, partition, weighting, explicit-zero, and validation semantics.
- Backend-specific setup and compatibility documentation.
- Safe, bounded degradation when storage is unavailable.
- No database error replacing a successful provider result.
- No credentials or sensitive bound values in logs.
- A conformance suite for custom implementations.

Switching from a local store to PostgreSQL begins using the history present in
PostgreSQL. It does not automatically import the local file. With empty or
unavailable global history, score-based routing remains available: a valid
empty aggregate supplies explicit zero rows and optimistic equal scores, while
an unavailable or invalid aggregate preserves the exact tie-break baseline.

## Performance and transparency risks

### Remote reads on provider selection

PR30 already requires server-side score aggregation and bounds result
cardinality by requested providers. A PostgreSQL implementation must preserve
that operation and add measured PostgreSQL-specific indexes, bounded connection
and statement timeouts, appropriate connection reuse, exact baseline fallback,
and returned-row/query-latency measurements.

Do not silently add caching, rollups, or materialized scores because they
change history freshness. If measured scale requires them, specify their
freshness and invalidation semantics in a separate change.

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

Until then, one immutable event table and three runtime operations do not justify a
universal ORM.

## Suggested PR boundaries

### PR13: storage versioning/shared-backend foundation — shipped

- Historically pinned the then-two-method runtime storage contract and
  dependency/import boundaries; PR30 later superseded only that method count.
- Added independent component versions beginning with `metrics = 1`.
- Added exact local runtime validation and shared named event conversion.
- Added typed read-only/create/offline-migrate administration and the standard CLI.
- Documented managed-backend connection/error/timeout expectations for PR14.
- Shipped no PostgreSQL, ORM/toolkit, reporting query, or aggregate query.

### PR30: storage-side score aggregation — shipped

- Made `query_score_aggregates` the mandatory third runtime operation.
- Added one bounded backend SQL query with exact partition/call-type semantics,
  explicit zero rows, one-reference-time flat/exponential weighting, and no raw
  fallback.
- Kept intermediate totals storage-neutral and `ProviderStats`/final scoring in
  shared Python.
- Shipped metrics v2 fresh-only, one measured SQLite index, no decorative
  DuckDB indexes, and no v1 migration/runtime overwrite path.
- Deferred PostgreSQL/Supabase, reporting, rollups/caching, retention,
  concurrency/lifecycle, buffering, and async work.

### PR25: durable local health

- Define a separate storage-neutral health-state interface.
- Implement the local DuckDB health backend.
- Preserve safe fallback to in-memory health.
- Do not overload `MetricsStore` with health semantics.

### PR14A: PostgreSQL organizational metrics — shipped (2026-08-07)

- Added optional `PostgresMetricsStore` for metrics as the live scoring source.
- Supplied explicit provisioning and Supabase deployment guidance.
- Added real PostgreSQL conformance/integration coverage and CI.
- Preserved DuckDB as the local default and SQLite as the native minimal option.
- Deliberately excluded provider health, so PR25 was not a prerequisite.

### PR14B: PostgreSQL provider health — remaining

- Add the PostgreSQL health backend against PR25's interface. PR25 first.

## Open decisions — resolved by PR14A

These were resolved during the PR14A requirements interview and by measurement.
Two departed from this document's own recommendations; both are noted.

1. **Driver: direct psycopg 3 with `psycopg_pool`, not SQLAlchemy Core.**
   *Departs from this document's recommendation.* The store's job is three SQL
   statements whose arithmetic must provably match two hand-written siblings,
   and keeping all three readable side by side was judged worth more than
   portability to an engine nobody has requested. Core's other advantages
   (pooling, transactions) are covered by `psycopg_pool` for one dialect with
   one schema version, and literal SQL makes the required `EXPLAIN` plan
   measurement direct. Alembic was declined for the same reason: the migration
   registry holds zero steps, so it would manage a history of one while adding
   a second version-history mechanism beside `nygen_router_schema_versions`.
2. **Versions:** `psycopg[binary]>=3.2,<4` and `psycopg-pool>=3.2,<4`, in a
   `postgres` extra. Verified against PostgreSQL 17.
3. **Constructor seams:** a `PostgresConfig` pydantic model carrying pool mode,
   the three timeouts, pool bounds, and TLS settings. No engine object is
   exposed.
4. **Physical types:** `TIMESTAMPTZ`, `BOOLEAN`, `DOUBLE PRECISION`, and `TEXT`
   for enum-like values — native PostgreSQL types rather than the local
   ISO-text representation, with UTC normalization preserving identical
   behavior. No PostgreSQL enum types.
5. **Schema validation:** once per store instance, inside lazy connection
   setup, exactly as the local backends do. No per-call network round trip.
6. **Supabase is the live scoring source**, not a reporting destination. No
   composite store, telemetry sink, or dual write exists.
7. **Timeout budgets:** latency-first defaults of connect 5 / statement 2 /
   checkout 2 seconds, with connect 10 / statement 5 / checkout 5 documented
   for distant links. The router holds its lock across storage calls, so these
   bound a remote database's effect on *all* routing, not just one call.
8. **Retention:** none. The event table stays append-only; retention needs its
   own freshness and durability semantics.
9. **Rollups/caching:** still unjustified. The bounded aggregate returns one
   row per requested provider, and the measured plan is an index scan.
10. **Component versions:** PostgreSQL reuses the same
    `nygen_router_schema_versions` table and independent component rows. No
    parallel migration history was introduced.

### Measured behaviors that shaped the design

- A managed pooler accepts the `options` startup parameter and **silently
  ignores it**, so a statement timeout set that way would leave every query
  unbounded while appearing configured. It is applied with a session `SET`.
- Supabase's pooler tolerates server-side prepared statements in transaction
  mode, unlike classic PgBouncer. `transaction_pooler` mode still disables them
  conservatively so self-hosted PgBouncer keeps working.
- `PostgresMetricsStore` holds **no lock of its own**, *departing from this
  document's "each bundled store serializes behind one per-instance lock"*.
  The local backends lock because their drivers cannot be shared across
  threads; a pool exists precisely to handle that, and a lock there would let
  one slow direct read block all routing.

## External references

- [SQLAlchemy Core and ORM architecture](https://docs.sqlalchemy.org/en/20/intro.html)
- [SQLAlchemy supported and external dialects](https://docs.sqlalchemy.org/en/20/dialects/)
- [Alembic migration documentation](https://alembic.sqlalchemy.org/en/latest/)
- [Supabase PostgreSQL connection modes](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [`duckdb-engine` SQLAlchemy dialect notes](https://pypi.org/project/duckdb-engine/)
