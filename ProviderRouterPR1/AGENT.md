# ProviderRouterPR1 Agent Guide

Implementation rules for this package:

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
- Do not add LangChain, Pydantic AI, Supabase, or OpenTelemetry yet. DuckDB
  shipped in PR4 as the metrics-storage default -- see below -- but it too
  follows the lazy-import rule: `storage/duckdb.py` never imports `duckdb` at
  module level, only inside method bodies, so the core import stays clean
  without it installed.
- Do not leak API keys in errors, logs, responses, or tests.
- Use typed models, not raw dictionaries, in core APIs -- `CallVariant`,
  `ProviderConfig`, `EligibilityResult`, etc. The one deliberate exception is
  `CallVariant.arguments`: it is intentionally an opaque `dict[str, object]`
  passed straight through to the provider SDK, never parsed or validated by
  router code (see the design principle below).
- Required tests must not require real API keys (a live provider test may exist,
  but it must skip when its key is unset).
- Only the `OPENAI_CHAT` protocol is implemented, dispatched dynamically:
  `CallVariant.operation` (e.g. `"chat.completions.create"`) is resolved via
  `getattr` against an `openai.OpenAI` client, not a hardcoded per-operation
  method map -- adding a new operation needs no adapter changes.
- Hard filters (eligibility) run before routing: enabled, not auth-benched, not
  in cooldown, resolvable API key, protocol has a registered adapter, and
  protocol has a matching `CallVariant` in this specific call. Apart from the
  two health checks (see "Provider health" below), they are static. There is
  currently no
  capability-based filtering (no `requires_tools`-style check) -- a provider
  that can't actually handle a call's `arguments` fails at call time like any
  other provider error, rather than being excluded pre-flight. Restoring
  pre-flight capability filtering (inferred from a call's own `arguments`,
  compared against `ProviderConfig.capabilities`) is a planned follow-up --
  see `Projectplan/ProjectPlan.md`, PR 21. Do not build that inference logic
  as part of unrelated work; it needs its own PR.
- A successful `invoke()` call returns the provider SDK's raw response object,
  completely untouched -- no wrapper, no `.attempts`/`.excluded` attached to
  it. Do not reintroduce a response wrapper; if per-call observability is
  needed later, that is PR 19's job (logging hooks), not a field on the return
  value.
- Round robin rotates among eligible providers, and a failed provider falls
  back to the next eligible one, re-picking whichever `CallVariant` matches
  the new provider's protocol. An auth failure (401/403) benches a provider
  for the rest of the run (`FilterReason.AUTH_DISABLED_THIS_RUN`), and a 429
  or repeated failures bench it temporarily (`FilterReason.IN_COOLDOWN`) --
  see "Provider health" below; a bad request (400/422), an unresolvable
  `operation`, or `arguments` that don't match the resolved operation's
  signature all stop the run immediately instead of trying more providers --
  these are call-shape problems every provider sharing that protocol would hit
  identically, so continuing would only bury the real cause. Health state
  lives on `ProviderRouter` so the filter and any policy can see it. When every
  tried provider fails, `RouterExhaustedError` enumerates each real failure
  rather than blending them.
- Every provider attempt (success or failure) is persisted as one
  `MetricsEvent` behind the swappable `MetricsStore` protocol -- see
  "Metrics persistence" below. Excluded providers are not recorded.
- Streaming calls are first-class: a stream that dies mid-generation falls back
  to the next provider, and its outcome is recorded when the stream ends rather
  than when it opens -- see "Streaming" below.
- Recorded attempts are summarized into per-provider `ProviderStats` by
  `aggregate_stats` (`stats.py`), split by call type -- see "Metrics
  aggregation" below. Two providers may not share a `name`: `__init__` rejects
  that with a `ConfigError` naming every duplicate.

## Design principle (native pass-through, non-negotiable)

The router chooses where a native API call is executed. It does not replace
the provider API with a generalized LLM interface:

- `CallVariant.arguments` is never inspected, validated, or translated beyond
  basic shape checks (it's a mapping; it doesn't already contain `"model"`).
  Whatever the caller puts in `arguments` goes to the provider's SDK call
  unchanged except for the injected `model` key.
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

## Metrics persistence (PR4)

`ProviderRouter` records one `MetricsEvent` per provider attempt so
score-based routing (PR7-10) has real history to work from:

- `MetricsStore` (`storage/base.py`) is a `typing.Protocol` with exactly two
  methods -- `record_attempt` and `query_recent`. Do not add aggregation,
  delete, or migration methods to it; aggregation happens in Python over
  `query_recent`'s output (PR7), never in per-backend SQL.
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
  installed) must never disturb a successful LLM response. Latency is one
  `time.perf_counter()` window around `adapter.invoke()`, recorded exactly as
  measured, on both success and failure.
- Do not build cross-process coordination for DuckDB, async/batched writes,
  schema versioning, or any query beyond `query_recent` -- all explicitly out
  of scope for PR4; see `Projectplan/ProjectPlan.md`'s PR4/PR7/PR13 sections.
- New columns are added to `_ADDED_COLUMNS` in `storage/base.py`, which every
  backend replays against its table on connect (check `PRAGMA table_info`, then
  `ALTER TABLE ADD COLUMN` whatever is missing). PR23 established this; PR6 and
  PR24 reuse it rather than inventing another mechanism. Added-column DDL must
  carry no constraints -- DuckDB rejects them in `ALTER TABLE ADD COLUMN`, even
  though it accepts them in `CREATE TABLE`.

## Metrics aggregation (PR7)

`aggregate_stats(events, provider_names, *, weight_fn=None)` in `stats.py`
turns recorded events into one `ProviderStats` per provider, for PR8's scoring:

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
- Every requested name gets an entry, including one with no events (counts `0`,
  rates and averages `None`), matching `health_report()`'s precedent: PR8's
  optimistic-start blend needs a real entry to fall back on.
- The four count fields are typed `float` although this PR only produces whole
  numbers -- reserved for PR10's fractional decayed weights. Do not "simplify"
  them to `int`. The three tallies stay `int`: exact, unweighted, diagnostic,
  never scored and never decayed.
- `weight_fn` exists but does nothing yet: it defaults to a flat 1.0 per event.
  Write every count, rate, and average through it as if weighting always
  mattered -- never a separate weighted and unweighted code path -- so PR10 can
  supply real decay without touching this signature. Do not implement decay
  here.
- No per-model grouping: a `ProviderConfig` fixes one model, so provider
  identity already implies model identity. No cost stat (PR6, deferred).
- Provider names are the identity key for both metrics and health, which is why
  `__init__` rejects duplicates. Renaming a provider therefore restarts its
  recorded history; that is accepted and documented in the README, not a bug to
  fix with fuzzy matching.

## Provider health (PR5)

`ProviderRouter` benches temporarily-bad providers so they stop being called,
extending the auth bench PR3 shipped. Never bench silently -- that is the whole
point of the feature:

- Transitions live on `ProviderHealthState` (`health.py`) as `record_failure`
  / `record_success`, not in the fallback loop; the loop only reports what
  happened and reads back whether a bench began. Router-side writes are
  get-or-create + mutate -- never replace the state object, which would
  silently zero an existing failure count.
- A 429 benches immediately without counting (flow control, not a broken
  provider). Timeout, connection, server error, and unknown are counted;
  crossing `failure_threshold` benches. The STOP categories (bad request,
  invalid operation) must never touch health -- the call is at fault, not the
  provider. Only a success resets the count, which is what makes a
  persistently broken provider cost one probe per cooldown window rather than
  three; do not "helpfully" reset the count on cooldown expiry.
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
- Bench logging (one slice pulled forward from PR19) dedups per bench episode:
  the first bench warns, repeat benches within that episode are DEBUG, and the
  first success logs one INFO recovery and re-arms the warning so a later,
  separate outage is not buried. `reset_health` drops the entry entirely, so a
  reset provider warns again.
- `reset_health()` must never touch the metrics store -- `MetricsStore` has no
  delete path and recorded history survives every reset. Reset means "may be
  tried again now" (hard filter), not "forget what happened" (scoring).
  An unknown provider name raises `ConfigError`; a typo'd reset that silently
  no-ops is the exact failure this feature exists to prevent.
- Health is in-memory and lives and dies with the router instance. Do not
  persist it, do not add per-provider `HealthConfig` overrides, escalating
  cooldowns, `Retry-After` handling, or any global give-up counter -- each was
  considered and deferred or rejected; see `Projectplan/ProjectPlan.md`'s PR5
  section.
- `ErrorCategory.STREAM_INTERRUPTED` counts toward `failure_threshold` and is
  never a STOP category: a chronically truncating provider is a broken
  provider, even though each of its calls starts out looking healthy.

## Streaming (PR23)

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
- On a stream row `latency_ms` always means time-to-first-chunk and is NULL
  when no chunk arrived; do not repurpose it. `total_duration_ms` is recorded
  for dead streams too. `stream` means a stream actually opened, so a streaming
  call that fails before that is recorded `stream=False`.
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
  max-restarts knob, or any async support -- all considered and deferred; see
  `Projectplan/ProjectPlan.md`'s PR23 section. Usage is exposed on the wrapper
  but persisted by nobody until PR24.
