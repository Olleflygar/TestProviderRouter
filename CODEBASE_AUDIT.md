# Nygen Router — Raised Issues

Date: 2026-08-06. Supersedes the 2026-07-28 audit: several of its findings have
since shipped (metrics identity via PR29, README rewrite, egg-info removal,
package directory rename, built-in OpenAI client reuse, and the PR13 local
versioned-schema/administration foundation).

Verification for this update: `ruff format --check`, `ruff check`, and strict
`mypy` over `src` all pass; 521 tests pass; configured branch coverage is 94%.
The previous secret audit found no secrets in the working tree or git
history, and `.env` remains ignored.

## 1. Major — not quick to fix, be prepared to discuss

- **No concurrency story.** Sync-only API, and shared mutable state is
  unguarded: `RoundRobinPolicy._index` (`policies/round_robin.py:25`), the
  health dict (`router.py:181`), cached SQLite/DuckDB connections. Docs are
  silent on whether one router may be shared across threads or used from async
  code. This becomes a correctness prerequisite before adding background
  writers or an async API; see PR31 and PR33 below.
- **Three names for one project:** repo `TestProviderRouter`, folder
  `ProviderRouter`, package `nygen-router`. Repo rename is cheap but
  outward-facing — deliberate decision, not a quiet fix.
- **Dead configuration surface:** `ProviderCapabilities` is settable but unread;
  `ANTHROPIC_MESSAGES` has no adapter. Both documented as planned (README hard
  filtering note; PR21 in `NewProjectPlan.md`) — defense is one link away.


## 2. Supervisor-requested storage and async optimization roadmap

### Finding A — raw history reads grow with event volume

`ScoreBasedPolicy.order()` currently asks `MetricsStore.query_recent()` for
every event in its 14-day default lookback, transfers all rows into Python, and
then calls `aggregate_stats()`. The work and returned data therefore grow with
the number of attempts even though scoring needs only one aggregate record per
eligible provider. This is acceptable for small local stores but is the wrong
default for an organization-wide PostgreSQL/Supabase store.

**PR30 — Storage-side score aggregation**

- Add a storage-neutral aggregate-query contract in PR30 on top of shipped
  PR13. Do not expose SQL rows, engines, or SQLAlchemy objects to policies.
- Return only the weighted counts, success rates, successful-attempt latency
  totals/weights, and diagnostic tallies required to build `ProviderStats`.
  Preserve the exact partition keys: metrics scope, provider ID, model,
  protocol, and call type.
- Preserve both flat lookbacks and exponential recency weighting. Define the
  timestamp used for a query once so a database query and Python fallback do
  not observe different `now` values.
- Keep a compatibility path for custom stores that only implement the current
  two-method `MetricsStore`; bundled remote stores must use the aggregate path.
- Add cross-backend semantic tests comparing SQL and Python aggregation,
  including empty providers, `NULL` latency, failures, streaming TTFT, and
  recency weighting. Add a 50k+-row benchmark and query-plan check. The hard
  acceptance criterion is bounded result cardinality proportional to eligible
  providers, not a machine-specific latency claim.
- Add/validate the smallest measured indexes needed by the aggregate query and
  record query latency plus rows returned so later regressions are visible.

Dependency: shipped PR13 defines the versioned schema and serialization seam. PR30 should land
before PR14 is treated as production-ready for live score-based routing.

### Finding B — one synchronous transaction per attempt is on the hot path

`ProviderRouter._record_metrics()` calls `record_attempt()` before returning a
regular provider response. SQLite commits every event individually, and a
future remote store would add a network round trip per physical attempt.
Although failures are best-effort and cannot replace the provider result, a
slow write can still delay that result until the storage timeout expires.

**PR32 — Opt-in bounded buffered/batched metrics writer**

- Add a wrapper or explicit public seam rather than changing every store's
  durability behavior silently. Synchronous writes remain the compatibility
  default until the new trade-offs are selected by the caller.
- Use a bounded in-process queue and one owned writer. Specify batch size,
  flush interval, queue-full policy, retry limit, ordering, duplicate handling,
  and whether process crashes may lose accepted-but-unflushed events.
- Add an optional bulk-insert capability so PostgreSQL can issue a real
  multi-row transaction. Falling back to repeated `record_attempt()` calls may
  move work off the caller thread but must not be described as database
  batching.
- Provide explicit `flush()` and idempotent `close()` behavior, deterministic
  shutdown, context-manager support, surfaced dropped-event counters, and
  warning/recovery behavior consistent with current best-effort writes.
- Test slow/failing stores, queue saturation, partial batch failure, shutdown,
  read-after-flush visibility, and process-exit behavior through public
  injection seams. Benchmark caller-visible latency and transaction count.

Dependency: PR31 must first establish connection/thread ownership. PR14 should
implement bulk insertion, after which PR32 can provide useful remote batching.

### Finding C — async opportunities exist, but `async def` wrappers are unsafe

The router, adapter protocol, built-in SDK client, stream wrappers, policies,
and metrics stores are synchronous. Declaring `ainvoke()` while calling these
same blocking methods would still block the event loop. The useful async I/O
boundaries are provider network calls, PostgreSQL reads/writes, waiting for
queue capacity/flush, and streaming iteration. Filtering, health arithmetic,
score calculation, and small in-memory operations do not benefit materially.

**PR31 — Concurrency and storage lifecycle contract**

- Document and test a support matrix for sharing a router/store across
  threads, async tasks, and processes before promising any of those modes.
- Define who owns and closes connections. The current SQLite and DuckDB stores
  cache one mutable connection; they must not be used concurrently without an
  explicit serialization or per-thread/per-operation connection design.
- Keep DuckDB's single-writer constraint explicit. Multiple agents/processes
  must not independently open the same DuckDB file for writes; use one owner
  process/writer, SQLite for a shared local file, or PostgreSQL for shared
  organizational state.
- Cover the router's other shared state too: round-robin position, health,
  warning flags, adapter cache, retry bookkeeping, and live stream state. A
  thread-safe metrics store alone does not make `ProviderRouter` thread-safe.
- Add real concurrent tests without monkeypatching internal collaborators,
  plus misuse errors where a mode remains intentionally unsupported.

**PR33 — Native async router and adapter path**

- Add an explicit async protocol and `ainvoke()`/async-stream contract; do not
  replace or implicitly alter the synchronous API.
- Use the provider SDK's async client and async iterators so cancellation,
  timeouts, fallback, same-provider retry accounting, stream restart, health,
  and raw-response identity have deliberate parity with the sync path.
- Define an async storage contract for PostgreSQL or a clearly bounded bridge
  for sync-only local stores. Never run blocking DuckDB/SQLite operations on
  the event-loop thread.
- Test cancellation before open and mid-stream, task concurrency, cleanup, and
  metric durability. Keep retry safety unchanged: opaque native arguments are
  not replay-safe merely because execution is async.

Dependency: complete PR31 first. PR33 is a larger feature and should follow
the storage performance work unless a measured application workload requires
async invocation sooner.

### Recommended delivery order for the storage track

1. **PR13 — shipped:** component schema versions, typed local administration,
   exact runtime validation, storage-neutral serialization, and documented
   remote timeout/error conventions. PostgreSQL and a general ORM were not
   added.
2. **PR30:** storage-side score aggregates so shared history returns
   O(eligible providers) summaries rather than O(recent events) rows.
3. **PR31:** concurrency, connection ownership, lifecycle, and supported-use
   contract. This is the safety gate for background writers and async work.
4. **PR14A:** optional `PostgresMetricsStore` against PostgreSQL/Supabase,
   explicit migrations, pooling/TLS/timeouts, aggregate reads, bulk writes,
   and real PostgreSQL CI. DuckDB remains the local default.
5. **PR32:** opt-in buffered/batched writes using PR14A's bulk capability.
6. **PR25, then PR14B:** first define durable health storage independently,
   then add its PostgreSQL implementation. If PR14 is kept as one PR rather
   than split into metrics and health, PR25 remains a hard prerequisite.
7. **PR33:** native async invocation and streaming after concurrency semantics
   are stable.

Supabase should use the standard PostgreSQL protocol; it is a deployment
target for `PostgresMetricsStore`, not a separate public storage abstraction.
The implementation must decide whether it is the live scoring source or only
an analytics destination. The latter requires a separately designed composite
store/telemetry sink rather than hidden dual writes.


## 3. Fixed (2026-08-04 to 2026-08-06)

Verified after the fixes and PR13: ruff format/check, strict mypy, and the test
suite all pass (521 passed, fully offline).

- **PR13 local storage foundation shipped (2026-08-06):** DuckDB and SQLite now
  create component-versioned `metrics = 1` schemas only at absent configured
  paths, validate every existing target read-only, and retain exact unversioned
  PR29 files as unstamped implicit baselines. A separate frozen typed admin API
  and `nygen-router storage inspect|create|migrate` CLI provide read-only
  diagnosis, overwrite-refusing creation, and explicit offline transactional
  migration with optional engine-safe validated backups. `MetricsStore` remains
  two methods. PR30 aggregation, PR31 concurrency, PR14 PostgreSQL/Supabase, and
  PR28 reporting remain separate.

- **Timezone bug in `query_recent`:** non-UTC timezone-aware `since` values
  (and event timestamps) are now normalized to UTC before the lexical ISO-text
  comparison (`storage/base.py`). Two regression tests added to the
  `test_metrics_store.py` conformance suite, covering both backends.
- **CI added:** `.github/workflows/ci.yml` runs ruff format/check, mypy, and
  the offline pytest suite on Python 3.12 and 3.13 for pushes to `main` and
  PRs. Unverified until first push.
- **Non-hermetic live test removed:** `test_live_provider.py` deleted — API
  keys are verified outside the test suite — so plain `pytest` is offline by
  construction. The interim `live`-marker gating went with it.
- **Tracked tool artifact removed:** `ProviderRouter/.claude/scheduled_tasks.lock`
  untracked (`git rm --cached`, deletion staged) and gitignored along with
  `settings.local.json`.
- **Built-in OpenAI clients are reused:** the router caches its built-in
  adapters and `OpenAICompatibleAdapter` lazily caches one SDK client per
  resolved API key. HTTP connection pools now survive across attempts while a
  corrected key still takes effect on the next attempt.

## 4. Deferred — small but needs a decision

- **Root `requirements-dev.txt` diverges from the package `[dev]` extra**
  (pytest-cov vs coverage, missing duckdb, unpinned). Delete or align — and
  which environment consumes it is unclear.
- **No LICENSE** — depends on whether the package is meant to be installable
  by others.
- **Doc duplication:** root README (349 lines) and package README (1104 lines)
  document the same features twice; trimming root to overview + link is an
  editorial pass.
