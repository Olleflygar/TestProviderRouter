# Nygen Provider Router — Updated Project Plan

## Project goal

Build `nygen-router`, a lightweight Python router that selects the best
configured provider for a user-chosen model. Routing decisions use observed
latency, reliability, rate limits, provider capabilities, and explicit
caller-defined metrics scopes.

Cost visibility is optional and based only on user-supplied pricing. Framework
adapters, dashboards, remote storage, and observability integrations remain
optional layers and must not become core import dependencies.

## Current implementation status

Git tags, repository history, and source confirm that PR1–5, PR7–10, PR12,
PR23, PR26, PR27, and the PR3R `CallVariant` redesign have shipped. PR29 was added
later as an unplanned corrective prerequisite for the remaining
metrics/storage roadmap. The old project plan contains 11 remaining roadmap
PRs: PR6 and PR13–22. PR11 and PR24 have been descoped and are recorded under
Scrapped PRs.

The roadmap below preserves those PR identifiers, revises overlapping scopes
where necessary, and adds four candidate PRs. It is ordered by recommended
implementation sequence rather than numerical PR order.

## Core working principles

These constraints define the architectural boundaries that shipped and future
work must preserve.

- `from nygen_router import ProviderRouter` must remain lightweight.
- Provider and framework dependencies are optional and lazily imported.
- Hard capability filters run before provider scoring.
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

## Recently shipped

This section gives additional implementation detail for the newest shipped
work. Older shipped PR details remain in `OldProjectPlan.md`.

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
nullable `request_size_bucket` field is reserved for compatibility and remains
NULL for router-produced events; the proposed PR11 producer was descoped.

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

### 1. PR13 — Storage versioning and shared-backend foundation

**Summary:** Prepare the storage layer for evolving schemas and managed
databases.

Add explicit schema migrations, remote connection handling, and reporting
queries while preserving storage-neutral public protocols. Database engines,
sessions, and raw SQL rows remain private implementation details.
Start from PR29's exact-schema/no-modification behavior; any future migration
must be explicitly versioned rather than reviving implicit check-and-ALTER.

### 2. PR22 — Pre-flight `CallVariant` validation

**Summary:** Validate operations and arguments before attempting a provider.

Resolve supported operations and check argument compatibility before entering
the fallback loop. Invalid calls fail without network traffic or misleading
provider-health changes.

### 3. PR21 — Automatic capability filtering

**Summary:** Exclude providers that cannot satisfy tools, streaming, or
structured-output requirements.

Inspect the relevant `CallVariant` arguments and compare inferred requirements
with provider capabilities. This restores the hard-filter behaviour removed by
the PR3R redesign while keeping interpretation limited to known protocol
fields.

### 4. PR25 — Durable local provider health

**Summary:** Persist provider cooldowns, rate limits, and health observations
across router lifecycles.

Introduce a storage-neutral health-state interface and a DuckDB-backed local
implementation. This first stage promises durable state on one installation,
not organization-wide coordination, and storage failures continue to degrade
safely to in-memory health.

### 5. PR14 — Postgres/Supabase organizational state

**Summary:** Share metrics and health across applications within an
organization.

Implement Postgres/Supabase backends for both metrics and provider-health state
using the interfaces established by PR13 and PR25. DuckDB remains the local
default; Postgres/Supabase provides the actual multi-application organizational
store.

### 6. PR15 — Routing profiles

**Summary:** Offer simple profiles for speed, reliability, or balanced routing.

Profiles configure the scoring weights and history settings that already exist
rather than introducing a separate scoring system. Cost is excluded from the
standard profiles, and capability requirements remain hard filters rather than
weighted preferences.

### 7. PR6 — Manual token-cost calculation

**Summary:** Estimate cost from user-supplied prices and recorded usage.

Add per-million-token pricing configuration and calculate estimated request
cost only from usage supplied through an explicit public seam. Pricing remains
visibility-only by default: no argument inspection, scraping, or automatic
cost influence on core routing.

### 8. PR28 — Optional local metrics dashboard

**Summary:** Provide a read-only single-page view of provider performance.

Show providers, scoped latency, success rates, rate-limit history, and any
explicitly configured cost estimates from the reporting interface. Ship it as
an optional local web extra so dashboard dependencies do not affect the core
import.

### 9. PR16 — Environment and configuration factories

**Summary:** Make common router setup concise and repeatable.

Add `ProviderRouter.from_env(...)` and `ProviderRouter.from_config(...)` while
retaining direct `ProviderConfig` construction. Factories may select routing
profiles and optional stores but must not hide invalid configuration or
silently enable sticky routing and retries.

### 10. PR19 — Configurable logging hooks

**Summary:** Expose routing decisions and failures through standard Python
logging.

Cover selection, filtering, success, failure, fallback, retries,
sticky-provider selections, and storage degradation. Existing internal warnings
should be consolidated into documented events without adding print-based
output.

### 11. PR20 — Optional observability hooks

**Summary:** Integrate routing activity with tracing and custom callbacks.

Provide optional OpenTelemetry, Logfire, and callback integrations using the
neutral metrics and event model. Observability failures and missing optional
packages must never break provider calls or the lightweight core import.

### 12. PR18 — Pydantic-AI adapter (very low priority)

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

### 13. PR17 — LangChain adapter (very low priority)

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

- PR29 is the shipped prerequisite for all remaining metrics/shared-storage
  work; later PRs preserve its scope and provider-partition identity.
- PR22 and PR21 follow PR13 in the implementation sequence.
- PR13, PR25, and PR14 form a staged persistence path: storage foundation,
  durable local health, then true shared organizational state.
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

- DuckDB provides local durability only; Postgres/Supabase provides
  organization-wide sharing.
- Any future usage or cost input is explicit and preserves the raw-response
  identity contract.
- `StickyRoutingPolicy` is configurable and opt-in. It supplies fixed provider
  preference only; it never learns affinity or guarantees provider-owned state
  continuity.
- `SameProviderRetryPolicy` is configurable and opt-in. Transient categories
  are retry candidates, never evidence that native replay is safe.
- Manual cost data is informational unless a later roadmap decision explicitly
  introduces cost-aware routing.
