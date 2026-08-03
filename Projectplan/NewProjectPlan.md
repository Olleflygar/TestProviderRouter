# Nygen Provider Router — Updated Project Plan

## Project goal

Build `nygen-router`, a lightweight Python router that selects the best
configured provider for a user-chosen model. Routing decisions use observed
latency, reliability, rate limits, request size, and provider capabilities.

Cost visibility is optional and based only on user-supplied pricing. Framework
adapters, dashboards, remote storage, and observability integrations remain
optional layers and must not become core import dependencies.

## Current implementation status

Repository history and source confirm that PR1–5, PR7–10, PR12, PR23, and the
PR3R `CallVariant` redesign have shipped. PR29 was added later as an unplanned
corrective prerequisite for the remaining metrics/storage roadmap. The old project plan contains 13
unshipped PRs: PR6, PR11, PR13–22, and PR24.

The roadmap below preserves those PR identifiers, revises overlapping scopes
where necessary, and adds four candidate PRs. It is ordered by recommended
implementation sequence rather than numerical PR order.

## Core working principles

- `from nygen_router import ProviderRouter` must remain lightweight.
- Provider and framework dependencies are optional and lazily imported.
- Hard capability filters run before provider scoring.
- A retryable provider failure falls back when another eligible provider
  remains.
- Storage, metrics, dashboard, logging, and observability failures must not
  invalidate a successful LLM response.
- Provider responses retain their native SDK identity unless a framework
  integration explicitly translates them at its own boundary.

## Recently shipped

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
nullable `request_size_bucket` field is reserved now and remains NULL for
router-produced events until PR11.

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

## Upcoming PRs

### 1. PR24 — Framework-neutral token usage instrumentation

**Summary:** Record provider-reported input, output, reasoning, and cache-token
metrics.

Replace the old OpenAI-only non-streaming extraction scope with a router-owned
usage record covering streaming and non-streaming responses. Provider adapters
extract available usage without mutating or wrapping the returned SDK object;
missing or unfamiliar usage must not break a successful call.

### 2. PR11 — Prompt-size metrics and routing buckets

**Summary:** Relate provider latency and reliability to prompt size.

Record actual input-token counts when available and use a documented estimate
before dispatch to classify requests into broad size buckets. Aggregation and
score-based routing can then compare providers within the relevant bucket
instead of blending small and large prompts.

PR29 already added nullable `MetricsEvent.request_size_bucket` and the database
column as a scoped exception to the former “columns arrive with their producer”
rule. PR11 must populate that existing field and owns estimation, bucket
values/boundaries, query filtering, aggregation, and bucket-aware scoring. It
must not add or migrate the column again.

### 3. PR26 — Configurable sticky routing

**Summary:** Allow users to opt into retaining a successful provider for future
calls.

Add an optional policy that prefers the current provider long enough to benefit
from provider-side prompt caching while that provider remains eligible and
healthy. The precise affinity and expiration configuration will be resolved
when constructing this PR's prompt; stickiness remains disabled unless
selected.

### 4. PR27 — Configurable same-provider retry policy

**Summary:** Control bounded retries before cross-provider fallback.

Keep this separate from sticky routing because retrying a failed request has
different latency and duplicate-work risks. Retry limits and eligible failure
categories are explicit configuration, with unsafe request and authentication
failures excluded and normal fallback preserved after the budget is exhausted.

### 5. PR13 — Storage versioning and shared-backend foundation

**Summary:** Prepare the storage layer for evolving schemas and managed
databases.

Add explicit schema migrations, remote connection handling, and reporting
queries while preserving storage-neutral public protocols. Database engines,
sessions, and raw SQL rows remain private implementation details.
Start from PR29's exact-schema/no-modification behavior; any future migration
must be explicitly versioned rather than reviving implicit check-and-ALTER.

### 6. PR22 — Pre-flight `CallVariant` validation

**Summary:** Validate operations and arguments before attempting a provider.

Resolve supported operations and check argument compatibility before entering
the fallback loop. Invalid calls fail without network traffic or misleading
provider-health changes.

### 7. PR21 — Automatic capability filtering

**Summary:** Exclude providers that cannot satisfy tools, streaming, or
structured-output requirements.

Inspect the relevant `CallVariant` arguments and compare inferred requirements
with provider capabilities. This restores the hard-filter behaviour removed by
the PR3R redesign while keeping interpretation limited to known protocol
fields.

### 8. PR25 — Durable local provider health

**Summary:** Persist provider cooldowns, rate limits, and health observations
across router lifecycles.

Introduce a storage-neutral health-state interface and a DuckDB-backed local
implementation. This first stage promises durable state on one installation,
not organization-wide coordination, and storage failures continue to degrade
safely to in-memory health.

### 9. PR14 — Postgres/Supabase organizational state

**Summary:** Share metrics and health across applications within an
organization.

Implement Postgres/Supabase backends for both metrics and provider-health state
using the interfaces established by PR13 and PR25. DuckDB remains the local
default; Postgres/Supabase provides the actual multi-application organizational
store.

### 10. PR15 — Routing profiles

**Summary:** Offer simple profiles for speed, reliability, or balanced routing.

Profiles configure the scoring weights and history settings that already exist
rather than introducing a separate scoring system. Cost is excluded from the
standard profiles, and capability requirements remain hard filters rather than
weighted preferences.

### 11. PR6 — Manual token-cost calculation

**Summary:** Estimate cost from user-supplied prices and recorded usage.

Add per-million-token pricing configuration and calculate estimated request
cost from PR24's neutral usage record. Pricing remains visibility-only by
default: no scraping and no automatic cost influence on core routing.

### 12. PR28 — Optional local metrics dashboard

**Summary:** Provide a read-only single-page view of provider performance.

Show providers, latency by call and prompt-size category, success rates,
rate-limit history, token usage, and configured cost estimates from the
reporting interface. Ship it as an optional local web extra so dashboard
dependencies do not affect the core import.

### 13. PR16 — Environment and configuration factories

**Summary:** Make common router setup concise and repeatable.

Add `ProviderRouter.from_env(...)` and `ProviderRouter.from_config(...)` while
retaining direct `ProviderConfig` construction. Factories may select routing
profiles and optional stores but must not hide invalid configuration or
silently enable sticky routing and retries.

### 14. PR19 — Configurable logging hooks

**Summary:** Expose routing decisions and failures through standard Python
logging.

Cover selection, filtering, success, failure, fallback, retries,
sticky-provider changes, and storage degradation. Existing internal warnings
should be consolidated into documented events without adding print-based
output.

### 15. PR20 — Optional observability hooks

**Summary:** Integrate routing activity with tracing and custom callbacks.

Provide optional OpenTelemetry, Logfire, and callback integrations using the
neutral metrics and event model. Observability failures and missing optional
packages must never break provider calls or the lightweight core import.

### 16. PR18 — Pydantic-AI adapter (very low priority)

**Summary:** Allow a Pydantic-AI agent to use the router as a model
implementation.

Pydantic AI expects its own model request/response, tool, streaming, and
`RequestUsage` behaviour, whereas the router accepts `CallVariant` objects and
returns native SDK responses. The optional adapter translates those contracts
and maps neutral usage into Pydantic AI types; it is not required for core
routing.

The router cannot necessarily be passed directly to a Pydantic-AI `Agent`
because it does not implement Pydantic AI's model interface. The integration
must import the router, while the router core must not import Pydantic AI.

### 17. PR17 — LangChain adapter (very low priority)

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

- PR24 is the framework-neutral instrumentation foundation; full framework
  adapters are not required to collect token metrics.
- PR11 follows PR24 so latency can be calibrated using observed token counts
  while still supporting pre-dispatch estimates.
- PR29 is the shipped prerequisite for all remaining metrics/shared-storage
  work; later PRs preserve its scope and provider-partition identity.
- PR22 and PR21 follow PR13 in the implementation sequence.
- PR13, PR25, and PR14 form a staged persistence path: storage foundation,
  durable local health, then true shared organizational state.
- PR15 is narrowed to profiles supported by the existing scoring model.
- Sticky routing and same-provider retries are separate PRs because they solve
  different problems and have different failure risks.
- PR6 is no longer merely dormant, but remains optional and follows token
  instrumentation.
- Pydantic AI precedes LangChain when integrations are eventually developed,
  but both remain at the bottom of the roadmap.
- Meeting logistics such as preparing review comments and scheduling the
  follow-up are project-management actions, not software PRs.

## Assumptions

- DuckDB provides local durability only; Postgres/Supabase provides
  organization-wide sharing.
- Usage extraction is best-effort and preserves the raw-response identity
  contract.
- Sticky routing is configurable and opt-in. Its detailed configuration is
  intentionally deferred to that PR's planning prompt.
- Manual cost data is informational unless a later roadmap decision explicitly
  introduces cost-aware routing.
