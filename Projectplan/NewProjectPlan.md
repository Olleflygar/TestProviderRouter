# Nygen Provider Router — Updated Project Plan

## Project goal

Build `nygen-router`, a lightweight Python router that selects the best
configured provider for a user-chosen model. Routing decisions use observed
latency, reliability, rate limits, provider health, and explicit caller-defined
metrics scopes.

Cost visibility is optional and based only on user-supplied pricing. Framework
adapters, dashboards, remote storage, and observability integrations remain
optional layers and must not become core import dependencies.

## Current implementation status

Repository history and existing tags establish the earlier release sequence;
current source and tests additionally confirm that PR1–5, PR7–10, PR12, PR13,
PR23, PR26, PR27, PR29, PR30, and the PR3R `CallVariant` redesign have shipped.
PR29 and PR30 were added later as corrective prerequisites for the remaining
metrics/storage roadmap. The old project plan's remaining active roadmap IDs
are PR6 and PR14–20. PR11, PR21, PR22, and PR24 have been descoped and are
recorded under Scrapped PRs.

The roadmap below preserves those PR identifiers, revises overlapping scopes
where necessary, and adds four candidate PRs. It is ordered by recommended
implementation sequence rather than numerical PR order.

## Core working principles

These constraints define the architectural boundaries that shipped and future
work must preserve.

- `from nygen_router import ProviderRouter` must remain lightweight.
- Provider and framework dependencies are optional and lazily imported.
- Provider-native `CallVariant.arguments` remain opaque to router code. The
  router does not infer capabilities or pre-validate SDK call signatures.
- A retryable provider failure falls back when another eligible provider
  remains, after any explicitly configured same-provider retry cycle.
- Storage, metrics, dashboard, logging, and observability failures must not
  invalidate a successful LLM response.
- Provider responses retain their native SDK identity unless a framework
  integration explicitly translates them at its own boundary.

## Shipped PRs

This is the concise release index reconstructed from the repository's Git tags,
commit history, source, and both project plans. PR4, PR5, PR7–10, PR12, PR23,
and PR29 have paired `pr*-start`/`pr*-complete` tags; PR1 and PR2 have completion
tags only. PR3 is marked shipped in the old plan and its implementation commit,
while PR3R is also represented by the `api-redesign` tag. The next section
retains detail for the most recent changes, while the old plan preserves
earlier implementation detail and historical rationale.

### [shipped] PR1 — Provider configs and real provider calls

Established validated provider configuration, API-key resolution, the first
OpenAI-compatible adapter, the router entry point, and real-call coverage.

### [shipped] PR2 — Essential hard filters

Added pre-routing eligibility checks with structured exclusion reasons and a
clear error when no configured provider can serve a call.

### [shipped] PR3 — Round robin with current-run memory

Added in-process round-robin selection, cross-provider fallback, failure
classification, and run-local authentication benching.

### [shipped] PR3R — `CallVariant` and native SDK pass-through redesign

Replaced the normalized request/response abstraction with protocol-specific
`CallVariant` inputs, official SDK dispatch, and untouched native responses.

### [shipped] PR4 — DuckDB-backed metrics storage

Introduced per-attempt metrics persistence behind a swappable `MetricsStore`,
with DuckDB as the local default and SQLite as an alternative.

### [shipped] PR5 — Health state and cooldowns

Added rate-limit and repeated-failure cooldowns, health reporting and reset
controls, and visible bench/recovery events.

### [shipped] PR7 — Metrics aggregation

Added pure per-provider statistics over recorded attempts, keeping regular
full-response latency separate from streaming time to first token.

### [shipped] PR8 — Basic score calculator

Added an explainable success-and-speed score with optimistic priors for
providers that have little or no history.

### [shipped] PR9 — Score-based routing policy

Connected stored history, aggregation, and scoring into a routing policy with
stable tie-breaking and safe fallback when metrics cannot be read.

### [shipped] PR10 — Recency weighting

Added optional half-life decay so recent observations can influence routing
more strongly without changing the default flat-history behavior.

### [shipped] PR12 — OpenAI Responses API adapter

Added native synchronous and streaming `responses.create` support, including
typed terminal-state handling and cross-protocol fallback.

### [shipped] PR23 — `RouterStream` streaming fallback and observation

Made stream outcomes observable at iteration time, with raw-chunk pass-through,
restart-or-raise behavior, health updates, TTFT metrics, and bounded fallback.

### [shipped] PR26 — Configurable fixed provider preference

Added the opt-in `StickyRoutingPolicy`, which tries configured provider IDs in
fixed order when eligible and delegates the non-sticky tail to round-robin,
score-based, or custom policy ordering.

### [shipped] PR27 — Configurable same-provider retry policy

Added a separate opt-in execution policy for bounded pre-open retries, with
FIRST, ALL, and SELECTED targeting, hard safety gates, and per-physical-attempt
health, metrics, and exhaustion accounting.

### [shipped] PR29 — Corrective metrics identity and history partitioning

Made `provider_id` and `metrics_scope` explicit, partitioned history by stable
call identity, and replaced implicit schema mutation with exact-schema checks.

### [shipped] PR13 — Versioned local schema and administration foundation

Added component-specific schema metadata, exact runtime validation, shared
named event conversion, typed read-only/create/migrate administration, and the
`nygen-router storage` CLI for DuckDB and SQLite. At PR13 release, normal
runtime initialized only an absent configured path and explicit offline
migration could stamp the exact implicit PR29 baseline without guessing at
unknown layouts. PR30 later retained the absent-path safety while replacing the
v1/stamping route with fresh-only v2 and no v1 migration.

### [shipped] PR30 — Storage-side score aggregation

Made `query_score_aggregates` the mandatory third `MetricsStore` operation,
moved bounded score reduction into backend-specific DuckDB/SQLite SQL, retained
shared Python `ProviderStats` and final-score calculation, and shipped fresh
metrics schema version 2 with measured backend-specific index choices. Raw
`query_recent` remains public but is no longer a score-policy path.

## Recently shipped

This section gives additional implementation detail for the newest shipped
work. Older shipped PR details remain in `OldProjectPlan.md`.

### PR30 — Storage-side score aggregation

**Shipped:** `MetricsStore` now requires `record_attempt`, raw-history
`query_recent`, and `query_score_aggregates`; legacy two-method custom stores
are rejected at router construction. For every non-empty metrics-enabled
ordering, `ScoreBasedPolicy` calls its tie-break policy once, captures one
reference time, issues exactly one aggregate call, validates a complete result,
and stably sorts the original baseline occurrences. There is no aggregate
configuration switch, per-provider query, or `query_recent` fallback.

DuckDB and SQLite automatically execute backend-specific parameterized SQL
against the exact lower-bound/scope/provider-ID/model/protocol/caller-declared
call-type partition and return one bounded row for every distinct requested
provider. The rows contain only intermediate attempt/success weights,
successful non-NULL-latency weight and weighted total, and exact unweighted
error/rate-limit/timeout tallies. Shared Python derives rates and averages,
builds the regular or streaming `ProviderStats` bucket from the current
provider configuration, and calls the unchanged pure score calculator.
`aggregate_stats` remains public and pure as the raw-history semantic oracle.

Flat weighting uses 1.0 per event over `lookback_hours`; exponential weighting
uses the latest six half-lives and
`0.5 ** (age_hours / half_life_hours)`. Both use the policy's one captured,
timezone-aware reference time. Failed latency never enters averages, while a
successful NULL latency adds success evidence without latency evidence. A
genuinely empty provider is returned as an explicit all-zero row and keeps the
optimistic start, so later success or failure self-corrects its score. Missing,
duplicate, unexpected, malformed, or inconsistent rows invalidate the entire
read and return the exact tie-break baseline (round robin by default); missing
data is never invented as optimistic zero history.

Fresh DuckDB and SQLite databases now use `metrics = 2`. SQLite retains one
`(provider_id, model, protocol, call_type, timestamp)` index used by both
current- and all-scope plans. DuckDB retains no score-query index because its
analytical plans stayed sequential scans with or without candidate ART
indexes. Versioned v1 and the exact implicit PR29/v1 baseline are recognized
read-only but rejected by normal runtime. PR30 deliberately provides no
v1-to-v2 migration; owners must stop writers and manually archive/delete the
disposable old target or configure an absent path. Normal runtime still never
overwrites, migrates, stamps, reindexes, redirects, or replaces an existing
database.

The standalone benchmark:

```sh
cd ProviderRouter
.venv/bin/python benchmarks/pr30_score_aggregation.py --rows 60000 --repetitions 7
```

generated 60,000 logical events and returned exactly 9 aggregate rows in every
query over 7 timed repetitions. SQLite's retained-index medians were
1.240208 ms current-scope and 2.090667 ms all-scope, versus
43.191167/43.610417 ms unindexed. A second scope-leading index reached
0.556125 ms for current scope but was rejected for its write/storage cost.
DuckDB no-index medians were 8.648875/9.006959 ms versus
8.202791/9.003167 ms with two ART candidates; all plans remained sequential
scans and the candidates increased seed/storage cost. These timings are
one-machine evidence, not universal latency promises.

After smoke validation, the two authorized disposable demo stores were
recreated empty at metrics v2: `~/.nygen_router/metrics.duckdb` discarded
2 rows and `WorkflowTests/workflow_history.duckdb` discarded 46 rows.
`WorkflowTests/workflow_history.pre-pr29.duckdb` was not touched.

PR30 did not add PostgreSQL/Supabase (PR14), reporting (PR28), rollups/caching,
retention, concurrency/lifecycle (PR31), buffered/batched writes (PR32), or
native async behavior (PR33).

### PR13 — Versioned local schema and administration foundation

**Historical shipped scope:** At PR13, fresh DuckDB and SQLite files
transactionally received the exact
`provider_attempts` schema plus `nygen_router_schema_versions` with the
independent `metrics = 1` component. Reopening a current database performs
read-only exact validation before normal I/O. The exact unversioned PR29 schema
was then an implicit version-1 baseline that runtime could read and write
without stamping; every incompatible, malformed, missing-table, or newer
existing target was left untouched with an actionable error.

The frozen typed administration records and `inspect_database`,
`create_database`, and `migrate_database` functions are separate from the
runtime `MetricsStore` protocol. The standard-library CLI exposes the same
implementation as `nygen-router storage inspect|create|migrate`. Inspection
remains read-only, create atomically refuses every existing target, and any
future migration remains offline, exclusive, route-validated, transactional,
idempotent, and optionally protected by an explicit engine-safe validated
backup.

PR30 superseded only PR13's two-method/v1 compatibility assumptions:
`MetricsStore` now has three mandatory methods, fresh local databases are
metrics v2, and v1/implicit-v1 targets have no approved PR30 migration and are
not runtime-compatible. PR13's separation of administration, independent
component versions, and no-runtime-overwrite safety remain intact. PR13 added
no remote backend, ORM/toolkit, reporting query, retention, health persistence,
or request-size metric.

### PR26 — Configurable fixed provider preference

**Shipped:** Added the optional `StickyRoutingPolicy` wrapper through the
existing `ProviderRouter(policy=...)` seam. It accepts a non-empty ordered
`list[str]` of canonical `provider_id` values, trims whitespace, rejects
non-strings, blanks, duplicates, and IDs unknown to the router, and retains a
defensive copy. Provider display names are never accepted as identity.

For each call, eligible configured sticky providers form a fixed prefix in the
declared ID order. The wrapped policy orders only the non-sticky eligible
remainder; omitting `fallback_policy` creates a fresh `RoundRobinPolicy`, while
`ScoreBasedPolicy` and custom policies remain supported. Hard eligibility and
health filtering always win. Retryable failures continue through the composed
order, STOP failures remain global fail-fast, and streaming restart consumes
the same precomputed order. Successful fallback never rewrites future
preference.

This is router-wide fixed preference, not learned affinity. It has no affinity
key, TTL, success history, persistence, cleanup, reset, or per-call override.
Calls compute their prefix independently and store no PR26 state; applications
should normally use separate policy instances per router because a wrapped
policy may itself be stateful. Fixed preference may improve provider-local
cache reuse or concentrate usage, but cannot guarantee either. Callers still
own strict endpoint/account affinity for provider-owned response IDs and state
because filtering or fallback can select another provider. Dedicated selection
logging remains with PR19 and observability hooks with PR20. Shipped PR27
same-provider retries compose independently and are never implied by sticky
preference.

### PR27 — Configurable same-provider retry policy

**Shipped:** Added optional `ProviderRouter(retry_policy=...)` independently of
the provider-ordering `policy=` seam. Omission and explicit `None` preserve the
former behavior of making no router-controlled same-provider retry. The
recommended `SameProviderRetryPolicy()` gives only ordered index zero up to
three total physical attempts, including its initial attempt; ALL and SELECTED
can give one bounded cycle to other distinct reached provider IDs. Values above
eight total attempts clamp to eight with one caller-facing `UserWarning`.

The built-in retries timeout, connection, and server-error categories only.
Bad request and invalid operation remain global fail-fast errors; auth and rate
limit bench then fall back; any newly started health bench ends the current
cycle. Every physical attempt independently updates health, metrics, scoring
history, and exhaustion diagnostics. Provider SDK retries stay disabled.

Streaming support is pre-open only. Once a `NormalizedStream` exists, PR27
state does not enter `RouterStream`; its existing restart-or-raise behavior
continues over the remaining precomputed provider order. There is no delay,
backoff, `Retry-After`, async support, persistent counter, schema change, or
per-call override.

Selecting retry is explicit router-wide acceptance of replay risk. The router
cannot prove a native request is idempotent: retry can duplicate provider work,
tools, stored/background operations, side effects, or charges. Native arguments
remain opaque, and caller-supplied idempotency mechanisms are passed through but
never created or verified.

### PR29 — Corrective metrics identity and history partitioning

**Shipped:** Replaced display-name metrics and health identity with required,
stable `provider_id`; allowed duplicate display names; required an explicit
router `metrics_scope`; and partitioned scoring history by scope, provider ID,
model, protocol, and caller-declared `CallType`.

`HistoryScope.CURRENT` reads the router's scope by default, while explicit
`HistoryScope.ALL` combines otherwise matching partitions across scopes.
Streaming telemetry now separates declared `call_type` from observed
`stream_opened`, preserving NULL TTFT when no first chunk arrives. The policy
automatically selects the invocation call type instead of exposing a separate
`use_streaming` setting.

The complete PR29 `provider_attempts` schema is created only for an absent
table. Incompatible existing tables are detected read-only and left untouched;
there is no automatic migration, backfill, deletion, or replacement. The
formerly reserved `request_size_bucket` field was removed from the event
record and schema (2026-08-05); the proposed PR11 producer was descoped.

### PR12 — OpenAI Responses API adapter

**Shipped:** First-class synchronous and streaming OpenAI Responses support.

`OPENAI_RESPONSES` is registered as a built-in protocol for native
`responses.create` calls. The adapter returns native SDK `Response` objects and
typed streaming events unchanged, observes completed/incomplete/failed terminal
states, and shares OpenAI client construction and exception mapping with Chat
Completions. Incomplete results are served with one visible warning and without
fallback or provider benching; declared failure events preserve their typed
details in `ProviderResponsesError`.

Bad requests and invalid operations remain intentional global fail-fast errors
across protocol variants, while retryable provider failures fall back between
Chat and Responses. Stored-resource lifecycle operations and provider-owned
response/conversation affinity remain caller responsibilities.

## Scrapped PRs

These proposals are intentionally excluded from the active roadmap. Their
numbers remain reserved so historical references continue to make sense.

### [scrapped] PR21 — Automatic capability filtering

Inferring tools, streaming, or structured-output requirements would require
the router to interpret provider-native argument keys and maintain semantic
knowledge of multiple SDK request formats. It would also encourage capability
metadata and provider-specific dependencies to spread into the routing core.

The router instead treats `CallVariant.arguments` as opaque pass-through data.
Eligibility remains limited to router-owned configuration and state. A provider
that cannot satisfy a native call reports that incompatibility through its SDK
at call time, using the existing failure classification and fallback behavior.

### [scrapped] PR22 — Pre-flight `CallVariant` validation

Resolving every operation and binding arguments against live SDK signatures
before routing would require optional provider SDKs to be loaded early and
would make the router validate data it otherwise promises to pass through
unchanged. SDK signatures can also be dynamic or differ across versions.

Operation and argument errors therefore remain adapter-time fail-fast errors.
They do not bench a provider, and the router does not add a separate pre-flight
inspection phase.

### [scrapped] PR24 — Framework-neutral token usage instrumentation

Token counting on the router's hot path is CPU-bound and can create memory
pressure when the router runs locally. A general implementation would also
need to inspect provider call arguments, adding semantic dependencies that
conflict with native pass-through and make the router less transparent.

The benefit does not justify that complexity: most providers let callers set
usage-related options directly in `CallVariant.arguments` and then expose usage
on their native response or stream. Users can continue to access that
provider-owned usage through the unchanged objects returned by the router.
Broader router-owned token instrumentation is deferred to a future roadmap
decision.

### [scrapped] PR11 — Prompt-size metrics and routing buckets

Estimating request size adds latency and requires interpreting opaque call
arguments. Even a lightweight heuristic must make ambiguous, provider-specific
assumptions about text, tools, files, URLs, base64 data, and multimodal inputs,
which conflicts with the router's lightweight and transparent scope.

The motivating use case—keeping unlike request sizes from sharing performance
history—is already covered more explicitly by `metrics_scope`. Callers know
their workload semantics and can place materially different requests in
different scopes without the router inspecting or guessing at their content.
See `../RequestSizeBuckets.md` for the compact design decision.

## Upcoming PRs

These are the remaining active proposals, ordered by recommended implementation
sequence rather than by PR number. Their scopes are directional until each PR
receives its own implementation prompt.

### 1. PR25 — Durable local provider health

**Summary:** Persist provider cooldowns, rate limits, and health observations
across router lifecycles.

Introduce a storage-neutral health-state interface and a DuckDB-backed local
implementation. This first stage promises durable state on one installation,
not organization-wide coordination, and storage failures continue to degrade
safely to in-memory health.

### 2. PR14 — PostgreSQL organizational state (including Supabase)

**Summary:** Share metrics and health across applications within an
organization.

Implement PostgreSQL backends for both metrics and provider-health state using
the interfaces established by PR13 and PR25. DuckDB remains the local default;
PostgreSQL provides the actual multi-application organizational store, with
Supabase as the initial managed deployment target.

Here, **Postgres** is the common short name for **PostgreSQL**, a specific
client/server relational database system; it is not a generic name for SQL
databases and is not middleware. **Supabase** is a managed platform whose
database is PostgreSQL, so the initial global metrics backend is a public,
optional `PostgresMetricsStore` that connects through the standard PostgreSQL
protocol and works with Supabase as well as other conventional PostgreSQL
deployments. It must not require the Supabase Data API or Supabase client SDK.

`PostgresMetricsStore` must implement all three mandatory `MetricsStore`
operations, including one bounded SQL aggregate call with PR30-equivalent
partition, weighting, cardinality, validation, and failure semantics. It uses
explicit schema versions and keeps any SQL toolkit, PostgreSQL driver, engine,
pool, and row representation private. PostgreSQL support is installed through
an explicit extra and selected through `metrics_store=`; merely importing or
using the local router must not load its dependencies. Direct and pooled
connection modes, SSL, bounded timeouts, least-privilege runtime access,
measured PostgreSQL indexes, and the extra network latency of score-history
reads and per-attempt writes must be documented and tested.

Selecting the global store does not synchronize an existing DuckDB or SQLite
file. It begins from the history already present in the selected PostgreSQL
database, and missing/unavailable history continues to use the routing
policy's documented tie-break behavior. Any future caching, background writes,
rollups, retention, or local-to-global replication requires separate explicit
semantics because each changes freshness, durability, or routing latency.

### 3. PR15 — Routing profiles

**Summary:** Offer simple profiles for speed, reliability, or balanced routing.

Profiles configure the scoring weights and history settings that already exist
rather than introducing a separate scoring system. Cost is excluded from the
standard profiles. Profiles do not inspect native arguments, infer provider
capabilities, or introduce new eligibility rules.

### 4. PR6 — Manual token-cost calculation

**Summary:** Estimate cost from user-supplied prices and recorded usage.

Add per-million-token pricing configuration and calculate estimated request
cost only from usage supplied through an explicit public seam. Pricing remains
visibility-only by default: no argument inspection, scraping, or automatic
cost influence on core routing.

### 5. PR28 — Optional local metrics dashboard

**Summary:** Provide a read-only single-page view of provider performance.

Show providers, scoped latency, success rates, rate-limit history, and any
explicitly configured cost estimates from the reporting interface. Ship it as
an optional local web extra so dashboard dependencies do not affect the core
import.

### 6. PR16 — Environment and configuration factories

**Summary:** Make common router setup concise and repeatable.

Add `ProviderRouter.from_env(...)` and `ProviderRouter.from_config(...)` while
retaining direct `ProviderConfig` construction. Factories may select routing
profiles and optional stores but must not hide invalid configuration or
silently enable sticky routing and retries.

### 7. PR19 — Configurable logging hooks

**Summary:** Expose routing decisions and failures through standard Python
logging.

Cover selection, filtering, success, failure, fallback, retries,
sticky-provider selections, and storage degradation. Existing internal warnings
should be consolidated into documented events without adding print-based
output.

### 8. PR20 — Optional observability hooks

**Summary:** Integrate routing activity with tracing and custom callbacks.

Provide optional OpenTelemetry, Logfire, and callback integrations using the
neutral metrics and event model. Observability failures and missing optional
packages must never break provider calls or the lightweight core import.

### 9. PR18 — Pydantic-AI adapter (very low priority)

**Summary:** Allow a Pydantic-AI agent to use the router as a model
implementation.

Pydantic AI expects its own model request/response, tool, streaming, and
`RequestUsage` behaviour, whereas the router accepts `CallVariant` objects and
returns native SDK responses. The optional adapter translates those contracts
and maps available provider usage into Pydantic AI types; it is not required
for core routing.

The router cannot necessarily be passed directly to a Pydantic-AI `Agent`
because it does not implement Pydantic AI's model interface. The integration
must import the router, while the router core must not import Pydantic AI.

### 10. PR17 — LangChain adapter (very low priority)

**Summary:** Allow the router to behave like a LangChain chat model.

LangChain expects `BaseChatModel` and Runnable methods, LangChain message
objects, streaming chunks, tool binding, callbacks, and
`AIMessage.usage_metadata`; the router does not directly implement those
conventions. The optional adapter translates those inputs and outputs without
making LangChain a core dependency.

Applications can use the router directly if they construct `CallVariant`
inputs and handle native provider responses themselves. The adapter is needed
only for drop-in participation in LangChain chains and agents, and LangChain
must import the router rather than the router importing LangChain.

## Ordering and boundary decisions

These decisions explain dependencies, sequencing, and scope boundaries that are
not obvious from the numbered roadmap alone.

- PR29, PR13, and PR30 are shipped prerequisites for the remaining
  metrics/shared-storage work. Later PRs preserve exact partition identity,
  mandatory bounded aggregate reads, independent version metadata, explicit
  administration, and no-runtime-migration boundaries.
- PR30 precedes production PostgreSQL score reads. PR31 separately owns
  concurrency and connection lifecycle.
- PR21 and PR22 are scrapped. Native arguments stay opaque, and operation or
  argument errors remain adapter-time fail-fast errors.
- PR13, PR25, and PR14 form a staged persistence path: shipped storage
  foundation, durable local health, then true shared organizational state.
- PR15 is narrowed to profiles supported by the existing scoring model.
- Shipped fixed-preference routing and same-provider retries remain
  separate because they solve different problems and have different failure
  risks.
- PR6 remains optional and may use only explicitly supplied usage data; it must
  not revive PR24-style argument inspection or router-owned token counting.
- Pydantic AI precedes LangChain when integrations are eventually developed,
  but both remain at the bottom of the roadmap.
- Meeting logistics such as preparing review comments and scheduling the
  follow-up are project-management actions, not software PRs.

## Assumptions

These statements capture the current roadmap's operating assumptions and should
be revisited when evidence or a dedicated PR changes them.

- DuckDB provides local durability only; PostgreSQL provides the shared
  client/server database, while Supabase is one managed PostgreSQL deployment
  option rather than a separate database abstraction layer.
- Any future usage or cost input is explicit and preserves the raw-response
  identity contract.
- `StickyRoutingPolicy` is configurable and opt-in. It supplies fixed provider
  preference only; it never learns affinity or guarantees provider-owned state
  continuity.
- `SameProviderRetryPolicy` is configurable and opt-in. Transient categories
  are retry candidates, never evidence that native replay is safe.
- Manual cost data is informational unless a later roadmap decision explicitly
  introduces cost-aware routing.
