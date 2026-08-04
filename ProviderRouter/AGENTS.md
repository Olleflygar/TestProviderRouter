# ProviderRouter Agent Guide

Implementation rules for this package:

- The current source and tests define shipped behavior. Use
  `../Projectplan/NewProjectPlan.md` for the current roadmap and
  `../Projectplan/OldProjectPlan.md` for historical rationale, in that order.
  The constraints below describe the current implementation. An explicitly
  scoped roadmap item may replace the corresponding current constraint, but
  unrelated work must not pull planned features forward.
- Core imports must stay lightweight: `from nygen_router import ProviderRouter`
  must never require any provider SDK to be installed.
- No provider SDK imports at module level, anywhere -- not even inside an
  adapter module. Always lazy-import inside the method body that actually
  needs it (see `adapters/openai_compatible.py`'s `import openai` inside
  `invoke()`). `httpx` is not a core dependency either; it appears only as an
  injectable test seam (`http_client`), as a transitive dependency of the
  `openai` extra, and in `_map_sdk_exception`'s in-body import, which is
  reached only after `openai` itself imported successfully. `router.py` stays
  entirely SDK-free -- no `openai`, no `httpx`, not even lazily.
- LangChain, Pydantic AI, Supabase, and OpenTelemetry remain optional roadmap
  layers and must not become core dependencies. DuckDB is the current
  metrics-storage default, but it follows the same lazy-import rule:
  `storage/duckdb.py` imports `duckdb` only inside method bodies, so the core
  import stays clean without it installed.
- Do not leak API keys in errors, logs, responses, or tests.
- Use typed models, not raw dictionaries, in core APIs -- `CallVariant`,
  `ProviderConfig`, `EligibilityResult`, etc. The one deliberate exception is
  `CallVariant.arguments`: it is intentionally an opaque `dict[str, object]`
  passed straight through to the provider SDK, never parsed or validated by
  router code (see the design principle below).
- Required tests must not require real API keys (a live provider test may exist,
  but it must skip when its key is unset).
- `OPENAI_CHAT` and `OPENAI_RESPONSES` are built-in protocols, dispatched
  dynamically: `CallVariant.operation` (for example
  `"chat.completions.create"` or `"responses.create"`) is resolved via
  `getattr` against an `openai.OpenAI` client, not a hardcoded per-operation
  method map. The supported Responses operation is synchronous and streaming
  `responses.create`; provider-resource retrieval, deletion, cancellation, and
  history remain caller-owned native SDK work.
- Hard filters (eligibility) run before routing: enabled, not auth-benched, not
  in cooldown, resolvable API key, protocol has a registered adapter, and
  protocol has a matching `CallVariant` in this specific call. Apart from the
  two health checks (see "Provider health" below), they are static. There is
  currently no
  capability-based filtering (no `requires_tools`-style check) -- a provider
  that can't actually handle a call's `arguments` fails at call time like any
  other provider error, rather than being excluded pre-flight. Restoring
  pre-flight capability filtering (inferred from a call's own `arguments`,
  compared against `ProviderConfig.capabilities`) is planned in
  `../Projectplan/NewProjectPlan.md` (PR21). Do not build that inference logic
  as part of unrelated work.
- A successful `invoke()` call returns the provider SDK's raw response object,
  completely untouched -- no wrapper, no `.attempts`/`.excluded` attached to
  it. Do not reintroduce a response wrapper; planned logging and observability
  hooks belong at their own boundaries, not in a field on the return value
  (see PR19 and PR20 in the current roadmap).
- Round robin rotates among eligible providers, and a failed provider falls
  back to the next eligible one after any explicitly configured same-provider
  retry cycle, re-picking whichever `CallVariant` matches the new provider's
  protocol. Omitting `retry_policy` preserves direct fallback with no
  same-provider retry. An auth failure (401/403) benches a provider
  for the rest of the run (`FilterReason.AUTH_DISABLED_THIS_RUN`), and a 429
  or repeated failures bench it temporarily (`FilterReason.IN_COOLDOWN`) --
  see "Provider health" below; a bad request (400/422 or an explicit Responses
  invalid-input code), an unresolvable `operation`, `arguments` that don't
  match the resolved operation's signature, or a missing SDK all stop the run
  immediately instead of trying more providers, including providers using
  another protocol variant. This global fail-fast rule keeps a broken preferred
  path visible instead of silently replacing it. Retryable failures still
  cross protocols. Health state lives on `ProviderRouter` so the filter and any
  policy can see it. When every tried provider fails, `RouterExhaustedError`
  enumerates each real failure rather than blending them.
- Every provider attempt (success or failure) is persisted as one
  `MetricsEvent` behind the swappable `MetricsStore` protocol -- see
  "Metrics persistence" below. Excluded providers are not recorded.
- Streaming calls are first-class: a stream that dies mid-generation falls back
  to the next provider, and its outcome is recorded when the stream ends rather
  than when it opens -- see "Streaming" below.
- Recorded attempts are summarized into per-provider `ProviderStats` by
  `aggregate_stats` (`stats.py`), split by call type -- see "Metrics
  aggregation" below. `provider_id` is canonical; IDs must be unique within a
  router, while duplicate display names are valid.

## Design principle (native pass-through, non-negotiable)

The router chooses where a native API call is executed. It does not replace
the provider API with a generalized LLM interface:

- `CallVariant.arguments` is never inspected, validated, or translated beyond
  basic shape checks (it's a mapping; it doesn't already contain `"model"`).
  Whatever the caller puts in `arguments` goes to the provider's SDK call
  unchanged except for the injected `model` key.
- Every `CallVariant` explicitly declares `CallType`. All variants in one
  invocation must agree. The declaration is router metadata: callers own its
  consistency with opaque native arguments.
- Adapters stay lightweight and make no request-shaping decisions of their
  own: the router resolves which `CallVariant` applies and injects `model`
  before calling into the adapter; the adapter's only job is dynamic dispatch
  and mapping the SDK's own exceptions onto the router's error hierarchy.
- The response returned to the caller is the provider SDK's real, typed
  object (e.g. `openai.types.chat.ChatCompletion`), not a normalized shape.

## Error transparency (non-negotiable)

Avoid the "peel the onion" debugging that plagues comparable routers:

- Every router error derives from `NygenRouterError`; the type names the stage
  that failed and the message names the provider and model. This holds even
  for caller/config mistakes discovered before any provider is contacted
  (`ModelArgumentConflictError`, `DuplicateCallVariantProtocolError`) and for
  dispatch failures inside the adapter (`UnsupportedOperationError`,
  `InvalidOperationArgumentsError`) -- never let a bare `AttributeError`,
  `TypeError`, or SDK exception escape unwrapped. The same holds inside a
  stream: only router errors leave `NormalizedStream.__next__`, and the
  synthesized `ProviderStreamInterruptedError` names the provider that
  truncated.
- Never swallow or re-message a provider/transport error. Surface the
  provider's verbatim message and structured fields (status, error type/code,
  body) -- note that `openai.APIStatusError.message` is an SDK-synthesized
  summary, not the provider's own text; pull the real message out of
  `exc.body` (see `_verbatim_message` in `adapters/openai_compatible.py`).
- If you add context, chain it (`raise ... from original`) and also keep the
  original on `.original`. Never wrap an already-wrapped router error again.
- Prefer common terminology: HTTP status + reason phrase, and the exact
  `openai`/`httpx` exception type name for transport and dispatch failures.

## Metrics persistence

`ProviderRouter` records one `MetricsEvent` per provider attempt so
score-based routing has real history to work from:

- `MetricsStore` (`storage/base.py`) is a `typing.Protocol` with exactly two
  methods -- `record_attempt` and `query_recent`. Do not add aggregation,
  delete, or migration methods to it in unrelated work; identity filters are
  scope, provider ID, model, protocol, and call type. Aggregation happens
  in Python over `query_recent`'s output, never in per-backend SQL. The planned
  storage-foundation work in PR13 may deliberately evolve the storage
  interfaces and migration design.
- `DuckDBMetricsStore` (`storage/duckdb.py`) is the default, pointed at
  `~/.nygen_router/metrics.duckdb`. It lazy-imports `duckdb` inside its
  methods only -- never at module level -- so the core import stays clean
  without the `duckdb` extra installed. Single-process by design (DuckDB
  allows one writing process per file); `SQLiteMetricsStore` is the
  recommended alternative for several local processes sharing one store.
- `ProviderRouter.__init__`'s `metrics_store` parameter distinguishes "not
  passed" (defaults to `DuckDBMetricsStore()`) from `metrics_store=None`
  (disables persistence entirely) via a private module-level sentinel in
  `router.py` -- `None` must keep meaning "off", so it cannot double as "use
  the default".
- Every `record_attempt` call from the router is wrapped in its own
  `try/except Exception` -- a storage failure (including `duckdb` not being
  installed) must never disturb a successful LLM response. Regular-call
  latency is measured around `adapter.invoke()`. Streaming latency is TTFT and
  stays `None` when no first chunk arrives; total duration still covers the
  complete streaming attempt, including pre-open failure.
- Every event carries the router's required nonblank `metrics_scope`, required
  `provider_id`, model, protocol, and declared call type. `provider_name` is
  display metadata and never an identity/filter key.
- Do not add cross-process coordination for DuckDB, async/batched writes,
  schema versioning, or queries beyond `query_recent` as unrelated work.
  Schema migrations, remote connection handling, and reporting queries belong
  to the explicitly scoped PR13 storage-foundation work in the current
  roadmap.
- PR29 deliberately removed automatic additive migration. A missing table is
  created with the exact current schema. An existing incompatible table is
  inspected read-only and left completely untouched; direct store users get an
  actionable mismatch error and router calls degrade safely. Runtime code must
  never alter, backfill, rename, delete, or replace a legacy database. PR13
  owns future explicit schema-versioning and migration design.
- `request_size_bucket` is reserved and nullable in the PR29 schema. Router
  events leave it `NULL`. PR11 is descoped: do not inspect opaque call
  arguments to populate it or add bucket-aware aggregation/scoring. Use
  caller-defined `metrics_scope` values to separate materially different
  workloads.

## Metrics aggregation

`aggregate_stats(events, providers, call_type, *, weight_fn=None)` in `stats.py`
turns recorded events into one `ProviderStats` per provider for scoring:

- Pure and query-only: a function over a list of `MetricsEvent`, with no store,
  router, or adapter involved and no window logic of its own. Callers filter
  with `query_recent` first. Do not push aggregation into per-backend SQL.
- Regular and streaming figures are separate fields and must never be blended:
  a regular latency is time-to-complete, a streaming one is time-to-first-chunk
  (see "Streaming" above), and they are decided successful at different
  moments.
- Averages cover successful attempts only -- a fast failure must never look
  fast -- and a success carrying no latency at all contributes nothing rather
  than a 0.0.
- Every requested provider ID gets an entry, including one with no events (counts `0`,
  rates and averages `None`), matching `health_report()`'s precedent: the
  optimistic-start blend needs a real entry to fall back on.
- The four count fields are typed `float` because recency weighting can make
  them fractional; with the default flat weighting they remain whole numbers.
  Do not "simplify" them to `int`. The three tallies stay `int`: exact,
  unweighted, diagnostic, never scored and never decayed.
- `weight_fn` controls how much each event contributes and is used by
  `ScoreBasedPolicy` for exponential recency decay. It defaults to a flat 1.0
  per event. Write every count, rate, and average through it; never create
  separate weighted and unweighted aggregation paths.
- Match every event exactly on provider ID, model, protocol, and invocation
  call type. A former display name does not prevent a stable ID from matching.
  Wrong-partition events affect no count, tally, average, or score.
- Cost statistics remain optional future work under PR6 and may depend only on
  usage supplied through an explicit public seam and user-supplied prices.
  PR24's router-owned token instrumentation is descoped.

## Score calculation

`calculate_provider_score(stats, weights, *, call_type=CallType.REGULAR)` in
`scoring.py` is a pure function from one `ProviderStats` record to one frozen,
explainable `ProviderScore`:

- Select only the regular or streaming bucket requested by `call_type`;
  never blend the two call types.
- Blend success rate toward `ScoreWeights.optimistic_start` using attempt count
  as evidence. Blend latency-derived speed quality toward the same prior using
  successful-attempt count as evidence.
- Compute `total` as the weighted average of the two components, never a sum.
  Exactly one zero component weight is valid; both zero is rejected.
- New providers need no branch or exploration toggle: the blend formula itself
  returns the optimistic start when the relevant evidence count is zero.
- `recent_error_count`, rate-limit counts, and cost are not scoring inputs.
- Keep this module independent of adapters, storage, providers, and I/O. It
  operates only on the values passed to it.

## Score-based routing

The `Policy` protocol is
`order(eligible: list[ProviderConfig], context: RoutingContext)`. The router
builds a fresh frozen context carrying the metrics store, scope, and declared
call type per `invoke()`; custom
policies must accept that second argument. `RoundRobinPolicy` accepts and
ignores it.

`ScoreBasedPolicy` connects metrics aggregation to score calculation:

- Call its tie-break policy exactly once on a copy of the eligible list before
  reading history. This is both the stable tie order and the graceful fallback
  order.
- Query the context's metrics store from `now - lookback_hours` on every call,
  aggregate only the rotated providers, calculate their selected call-type
  scores, and use one stable descending sort. Do not cache scores or add
  tie-grouping logic.
- If metrics are disabled, the eligible list is empty, or the history query
  fails, return the tie-break order unchanged. Query failures warn once per
  policy instance and log repeats at DEBUG.
- `ScoreBasedPolicy` reads the call type from `RoutingContext`; there is no
  independently configurable `use_streaming` setting.
- `HistoryScope.CURRENT` (default) queries only the router's scope;
  `HistoryScope.ALL` deliberately omits only the scope filter. Both make one
  query and still match ID, model, protocol, and call type in Python.
- `half_life_hours=None` preserves flat `lookback_hours` behavior exactly. A
  positive half-life replaces that window entirely: query the
  latest six half-lives and pass exponential event weights
  `0.5 ** (age_hours / half_life_hours)` to aggregation. The multiplier six is
  fixed, and the same half-life applies to every provider and metric.
- Decay affects scoring evidence only. The exact diagnostic error,
  rate-limit, and timeout counts remain unweighted.

## Fixed provider preference

`StickyRoutingPolicy` is the shipped, opt-in PR26 wrapper around another
`Policy`. It stores a validated fixed list of canonical provider IDs, never
learned affinity or outcome state:

- Hard filtering happens first. Eligible sticky IDs lead in configured order;
  disabled, keyless, unsupported, protocol-mismatched, or benched providers
  cannot be reintroduced.
- The wrapped policy is called exactly once with a fresh list containing only
  the eligible non-sticky remainder and the unchanged `RoutingContext`.
  Its ordering, omissions, and duplicates are preserved, but foreign provider
  IDs raise `ConfigError` before adapter invocation.
- Omitting `fallback_policy` creates a fresh `RoundRobinPolicy` per sticky
  policy. Score-based and custom wrapped policies remain supported.
- Configuration requires a non-empty `list[str]` of configured `provider_id`
  values. Values are trimmed and copied; non-strings, blanks, duplicates, and
  unknown IDs are rejected. Display names are never identity.
- Successful fallback does not rewrite preference. Regular and streaming calls
  use the same composed order and existing fallback, STOP, health, metrics, and
  raw pass-through behavior.
- There is no affinity key, TTL, clock, persistence, cleanup, reset, per-call
  override, sticky-specific logging, or observability schema. PR19/PR20 own
  those signals. Shipped PR27 retry composes independently and is never implied
  by sticky preference.

## Same-provider retry

`SameProviderRetryPolicy` is the shipped, opt-in PR27 execution policy. It is
separate from provider-ordering `Policy` implementations and selected only
through `ProviderRouter(retry_policy=...)`:

- Omission and explicit `None` mean no router-controlled same-provider retry.
  Provider SDK retries remain disabled.
- `max_attempts` is total physical attempts in one targeted cycle, including
  the initial ordered attempt. The default is 3 and the effective maximum is
  8; larger values clamp with one caller-facing `UserWarning`.
- `FIRST` targets ordered index zero, `ALL` the first reached occurrence of
  every distinct eligible provider ID, and `SELECTED` only reached configured
  canonical IDs. Filters and custom-policy omissions are never bypassed;
  deliberate ordering duplicates remain base occurrences but do not gain new
  cycles.
- The built-in category set is exactly timeout, connection, and server error.
  Bad request and invalid operation stop globally; auth and rate limit bench
  then fall back. A newly triggered health bench or the effective ceiling ends
  the cycle. A trusted custom policy may select pre-open unknown failures but
  cannot bypass hard gates and must return exactly `bool`.
- Construct one adapter per ordered occurrence and reuse it for that
  occurrence's retries. Build a fresh top-level argument dict and measure,
  record metrics, update health, and preserve the exact error for every
  physical attempt.
- `RetryContext` is frozen and contains no provider configuration, API key,
  arguments, adapter, metrics store, or mutable health/fallback state. Router
  counters remain local to one synchronous `invoke()` call; built-in policy
  configuration is frozen and reusable across concurrent calls.
- Streaming retry is pre-open only. Once an adapter returns a
  `NormalizedStream`, no PR27 state enters `RouterStream`, including for empty
  or interrupted streams and pre-open failures encountered during a later
  stream restart.
- Never claim retry safety. A timeout or disconnect does not prove the provider
  did not process the request. Replay can duplicate work, tools, side effects,
  stored/background operations, or charges. Native arguments remain opaque;
  idempotency mechanisms pass through but are neither created nor verified.
- There is no per-call override, delay, backoff, jitter, `Retry-After`, async
  support, persistent retry state, retry metrics/schema, or response wrapper.
  PR19/PR20 still own general logging and observability hooks.

## Provider health

`ProviderRouter` benches temporarily-bad providers so they stop being called,
including after authentication failures. Never bench silently -- that is the
whole point of the feature:

- Transitions live on `ProviderHealthState` (`health.py`) as `record_failure`
  / `record_success`, not in the fallback loop; the loop only reports what
  happened and reads back whether a bench began. Router-side writes are
  get-or-create + mutate -- never replace the state object, which would
  silently zero an existing failure count.
- A 429 benches immediately without counting (flow control, not a broken
  provider). Timeout, connection, server error, stream interruption, and
  unknown are counted; crossing `failure_threshold` benches. The STOP
  categories (bad request,
  invalid operation) must never touch health -- the call is at fault, not the
  provider. Only a success resets the count, which is what makes a
  persistently broken provider cost one probe per cooldown window rather than
  three; do not "helpfully" reset the count on cooldown expiry.
  With retries enabled, every physical failure contributes independently and a
  newly reached threshold stops that provider's remaining retry cycle.
- `HealthConfig` (`health.py`, exported from the package root) is validated at
  the constructor boundary via `HealthConfig.model_validate`, so a dict typo
  raises immediately and no raw dict flows past `__init__`.
- All cooldown arithmetic runs on `ProviderRouter`'s injected `clock`
  (defaulting to `time.monotonic`) -- never `time.time()`, `datetime.now()`, or
  a sleep. It is a constructor seam like `adapter_factory`/`policy`; tests
  inject a fake clock and advance it. `MetricsEvent` timestamps are unrelated.
- `filter_eligible_providers()` is strictly read-only over health state: an
  expired cooldown simply reads as eligible and is never cleared there.
  Benching and clearing are the router's business.
- `FilterReason.IN_COOLDOWN` is one member for both triggers; the detail string
  tells them apart and carries remaining seconds plus the provider's verbatim
  last error, so a fully-benched router still enumerates root causes. The
  trigger is stored on the state when the bench is taken -- do not infer it
  from the failure count, which a 429 neither increments nor resets.
- Bench logging deduplicates per bench episode:
  the first bench warns, repeat benches within that episode are DEBUG, and the
  first success logs one INFO recovery and re-arms the warning so a later,
  separate outage is not buried. `reset_health` drops the entry entirely, so a
  reset provider warns again.
- `reset_health()` must never touch the metrics store -- `MetricsStore` has no
  delete path and recorded history survives every reset. Reset means "may be
  tried again now" (hard filter), not "forget what happened" (scoring).
  An unknown provider ID raises `ConfigError`; a typo'd reset that silently
  no-ops is the exact failure this feature exists to prevent.
- Health is in-memory and lives and dies with the router instance. Do not
  persist it as unrelated work, and do not add per-provider `HealthConfig`
  overrides, escalating cooldowns, `Retry-After` handling, or any global
  give-up counter. Durable local health is separately planned in PR25; the
  other alternatives were considered and deferred or rejected in
  `../Projectplan/OldProjectPlan.md`.
- `ErrorCategory.STREAM_INTERRUPTED` counts toward `failure_threshold` and is
  never a STOP category: a chronically truncating provider is a broken
  provider, even though each of its calls starts out looking healthy.

## Streaming

A stream's real outcome happens after `invoke()` has returned, inside the
consumer's own loop, so the returned object is the only place the router can
still act:

- The SDK-specific knowledge stays behind the adapter boundary.
  `NormalizedStream` (`adapters/base.py`) is an ABC, not a `Protocol` -- the
  router needs a reliable `isinstance` -- and `OpenAICompatibleAdapter.invoke`
  wraps an `openai.Stream` in `OpenAIChatStream` before returning. An adapter
  returning a raw SDK stream still passes through untouched; returning a
  `NormalizedStream` is the documented opt-in.
- `_map_sdk_exception` is the single copy of the SDK-exception mapping, shared
  by `invoke()` and the stream wrapper. It carries `httpx` branches because
  during SSE iteration raw transport errors escape unwrapped -- the SDK only
  folds them into its own types around `.create()`. The dispatch-specific
  `TypeError`/`AttributeError` handling stays in `invoke()` alone: a mid-stream
  `TypeError` must never be reported as the caller's arguments being wrong.
- Responses streams yield native typed events unchanged.
  `response.completed` and `response.incomplete` are terminal served results;
  incomplete results warn once and do not trigger fallback or benching.
  `response.failed` and `error` become `ProviderResponsesError` with their
  native event/response and structured fields retained. Any recognized
  Responses stream that ends without a terminal result is silently truncated;
  a genuinely unfamiliar event shape retains the general unrecognized-shape
  behavior below.
- `RouterStream` (`router.py`) is a pass-through iterator. Never buffer,
  reorder, or synthesize chunks, and never yield a router-invented marker
  object. Per-chunk work stays at one completion check.
- Recording happens at stream end, in metrics and in health alike. Recording
  success at open would let a provider whose streams open fine and die
  mid-generation oscillate and never reach `failure_threshold`.
- A fully consumed stream that yields zero chunks is
  `ProviderStreamInterruptedError`, regardless of its reported completion or
  recognized-shape state: it produced no usable response. Record it as a
  streaming failure with NULL TTFT, apply the normal health transition, and
  obey `stream_failure_policy` (`RESTART` by default, `RAISE` when configured).
- For declared streaming calls, `latency_ms` always means time-to-first-chunk
  and is NULL when no chunk arrived; do not repurpose it. `total_duration_ms`
  is recorded for completion or failure, including pre-open failure.
  `stream_opened` is false before a `NormalizedStream` return and true after;
  regular calls use `None`.
- A truncation verdict needs evidence: only a stream whose chunk shape the
  wrapper recognized can be called silently truncated. An unrecognized shape
  logs one WARNING per operation per router and counts as completed.
- `close()` is idempotent, terminal, and never restarts. It records the success
  if the completion marker was already seen and records nothing otherwise --
  the one documented exception to one event per attempt.
- The rule-carrying steps (metrics recording, health transitions, failure
  classification, per-provider argument building) each exist exactly once as
  helpers on `ProviderRouter`; `invoke()` and `RouterStream` keep two thin,
  separate loops. Do not rewrite them into one attempt engine.
- Do not add per-call overrides for `stream_failure_policy` or `on_restart`, a
  max-restarts knob, or any async support as unrelated work -- all were
  considered and deferred in `../Projectplan/OldProjectPlan.md`. Usage is
  exposed on the wrapper but is not persisted by the router. PR24 is descoped;
  do not revive usage extraction or argument inspection as unrelated work.
