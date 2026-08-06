# nygen-router

`nygen-router` is a lightweight foundation for routing a native, provider-specific
API call to one of several providers that can serve the same model. It does not
translate your request into a shared internal schema: you supply the exact
arguments the provider's own SDK expects, the router picks an eligible provider,
inserts the configured model identifier, and returns the provider's response
exactly as its SDK returned it. The router validates provider configuration,
filters out providers that cannot satisfy the call (hard filters), rotates
between the eligible providers (round robin), and falls back to another eligible
provider when one fails.

OpenAI Chat Completions and OpenAI Responses are built in, both dispatched via
the official `openai` Python SDK and usable against an OpenAI-compatible
`base_url`, not just OpenAI itself. The supported Responses operation is
synchronous or streaming `responses.create`. Every provider attempt is recorded
as an observational metrics event behind a swappable `MetricsStore` (DuckDB by
default, SQLite as a fully-supported alternative) -- see "Metrics persistence"
below. Metrics aggregation, score calculation, score-based routing, recency
weighting, configurable fixed provider preference, optional same-provider
retry, provider health, and streaming fallback are implemented.
Token usage remains available on native provider responses and streams, while
router-owned token instrumentation is descoped. Additional storage layers,
provider-resource management, and framework adapters remain planned or
caller-owned work.

The source and tests define shipped behavior. See
[`../Projectplan/NewProjectPlan.md`](../Projectplan/NewProjectPlan.md) for the
current roadmap and
[`../Projectplan/OldProjectPlan.md`](../Projectplan/OldProjectPlan.md) for
historical design rationale. When they disagree, the source and tests take
precedence, followed by the current roadmap.

## Local Development

Requires **Python 3.12+**. Do not use a conda env on Python 3.10 for this package.
From the repository root:

```sh
cd ProviderRouter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run tests with pytest (not by executing test files directly):

```sh
pytest
pytest tests/test_config.py -v
```

If you see `ModuleNotFoundError: No module named 'nygen_router'`, you are likely
using the wrong Python (for example conda's `provider-router` instead of
`.venv/bin/python`). Select the `.venv` interpreter in your IDE, or run:

```sh
.venv/bin/python -m pytest
```

## Minimal Usage

```python
from nygen_router import ApiProtocol, CallType, CallVariant, ProviderConfig, ProviderRouter

router = ProviderRouter(
    providers=[
        ProviderConfig(
            provider_id="provider-a-production",
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="my-model",
            base_url="https://api.provider-a.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        )
    ],
    metrics_scope="my-application:production",
)

response = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.REGULAR,
            arguments={"messages": [{"role": "user", "content": "Hello"}]},
        )
    ]
)
print(response.choices[0].message.content)
```

`response` is the real `openai.types.chat.ChatCompletion` object the winning
provider's SDK returned -- nothing is parsed, wrapped, or re-shaped. Do not
include `"model"` in `arguments`: the router always injects the selected
provider's configured `model` itself, and raises before contacting any provider
if `arguments` already has one.

## OpenAI Responses API

Configure a Responses endpoint with `OPENAI_RESPONSES` and call only the
supported model-execution operation, `responses.create`:

```python
router = ProviderRouter(
    providers=[
        ProviderConfig(
            provider_id="provider-a-responses-production",
            name="provider_a",
            protocol=ApiProtocol.OPENAI_RESPONSES,
            model="my-model",
            base_url="https://api.provider-a.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        )
    ],
    metrics_scope="my-application:production",
)

response = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_RESPONSES,
            operation="responses.create",
            call_type=CallType.REGULAR,
            arguments={
                "input": "Explain why the sky appears blue.",
                "instructions": "Answer in two sentences.",
            },
        )
    ]
)
print(response.output_text)
```

The return value is the original `openai.types.responses.Response`. Native
output items, function calls, tool results, structured output, reasoning fields,
and usage remain available exactly as the installed SDK provides them. The
router does not translate Chat `messages` into Responses `input`, normalize
tools, or inspect any other native argument.

Streaming also returns the provider SDK's typed events unchanged:

```python
stream = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_RESPONSES,
            operation="responses.create",
            call_type=CallType.STREAMING,
            arguments={"input": "Count from one to three.", "stream": True},
        )
    ]
)

for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="")

if stream.usage is not None:
    print(stream.usage.total_tokens)
```

The native API differences remain visible:

| Behavior | Chat Completions | Responses |
| --- | --- | --- |
| Request content | `messages` | `input` |
| Stream items | `ChatCompletionChunk` | typed Responses events |
| Success marker | non-null `finish_reason` | `response.completed` |
| Served partial result | terminal finish reason such as `length` | `response.incomplete` |
| Stream usage | optional final usage chunk | terminal event's `response.usage` |
| Native text access | `response.choices[...]` | `response.output_text` and `response.output` |

`response.incomplete` is terminal and served, including reasons such as
`max_output_tokens` and `content_filter`. The router returns or yields it,
records success under the current binary metrics contract, and emits exactly
one standard-logging warning naming the provider, model, response ID, and reason.
It does not retry, fall back, or bench the provider. Unknown future reasons are
reported without being rejected.

`response.failed` and `error` are provider-declared failures. They become
`ProviderResponsesError`, which preserves the typed event, embedded response,
provider code, message, and parameter. Clearly retryable codes use the normal
fallback and health rules; invalid input codes remain global fail-fast errors.
Queued or in-progress background responses pass through natively.

Only `responses.create` is routed. Retrieval, deletion, cancellation, input-item
listing, history, and background lifecycle management remain the caller's
responsibility through the exact provider's native SDK client. Response IDs and
conversation IDs belong to the endpoint/account that created them.
`previous_response_id`, conversations, stored responses, and background calls
may pass through `responses.create`, but the router does not promise that a
later call will pick the same provider. `StickyRoutingPolicy` can make that
provider a best-effort fixed preference, but filtering and fallback may still
select another endpoint/account. Preserve strict provider affinity yourself for
stateful continuation; stateless calls are safe across interchangeable providers.

## Installing the openai SDK

The core package (`from nygen_router import ProviderRouter`) never requires any
provider SDK -- that import always works with just `pydantic` installed. The
`OPENAI_CHAT` and `OPENAI_RESPONSES` adapters lazily import `openai` only when
actually invoked, so install the matching extra to use either:

```sh
pip install "nygen-router[openai]"
```

```sh
export PROVIDER_A_API_KEY="your-key"
```

API keys can also be passed explicitly with `api_key`, but keys are never printed
or included in router errors.

The built-in adapters build one SDK client per provider and reuse it -- along
with its pooled HTTP connections -- across calls, retries, and fallbacks for
the router's lifetime, so only a provider's first request pays the TCP/TLS
handshake. Connections are released when the process exits. The client is
rebuilt only when the provider's resolved API key changes, so a key corrected
mid-run still takes effect on the next call. A custom `adapter_factory` is
still called per attempt and owns its own client reuse.

The recommended batteries-included install adds DuckDB too, so metrics
persistence (see below) works out of the box:

```sh
pip install "nygen-router[openai,duckdb]"
```

## Hard filtering

Before routing, the router filters the full provider list down to providers that
can satisfy this call. Filters are hard, not scores: a provider that fails an
essential check is excluded, not ranked lower:

- **disabled** -- `ProviderConfig(enabled=False)`
- **auth-benched** -- an auth failure benched it for the rest of this run (see
  [Provider health and cooldowns](#provider-health-and-cooldowns))
- **in cooldown** -- it was temporarily benched after rate limiting or repeated
  failures (same section)
- **no API key available** -- neither `api_key` nor a populated `api_key_env`
- **unsupported protocol** -- no adapter registered for this protocol at all
- **no matching `CallVariant`** -- the provider's protocol has an adapter, but
  this specific `invoke()` call didn't supply a `CallVariant` for it

The router intentionally does not infer whether a provider supports what a
call's native `arguments` request (tool calls, streaming, JSON mode, etc.).
`ProviderConfig` has no capability declaration: keeping one would imply that
the router understands provider-specific argument semantics. An incompatible
call is rejected by the provider SDK at call time and follows the documented
failure classification. PR21 capability inference and PR22 SDK-signature
pre-validation were scrapped to preserve this pass-through boundary.

If filtering removes every configured provider, `invoke()` raises
`NoEligibleProvidersError`, whose message enumerates each excluded provider with
its own specific reason rather than a single blended summary.

```python
try:
    response = router.invoke([...])
except NoEligibleProvidersError as error:
    print([(e.provider_id, e.provider_name, e.reason) for e in error.exclusions])
```

A successful call returns only the provider's raw response -- there is no
`.attempts`/`.excluded` on it. If you need to see which providers were tried
before a call ultimately failed, see `RouterExhaustedError` below.

## Round robin and fallback

The router rotates between eligible providers across successive `invoke()` calls
(round robin), so load is spread rather than always hitting the first provider.
Rotation is per-process only -- there is no persistence across restarts yet.

When a selected provider fails, the router falls back to the next eligible
provider (picking whichever `CallVariant` matches that provider's protocol)
instead of failing the whole call. Failures are classified to decide what
happens next:

- **Timeout, rate limit (HTTP 429), connection failure, server error (5xx), or
  unknown** -- try the next eligible provider. These also feed provider health,
  which may bench the failing provider from *later* calls (see
  [Provider health and cooldowns](#provider-health-and-cooldowns)).
- **Auth (HTTP 401/403)** -- record the failure, try the next provider, and bench
  the failing provider for the rest of this process. It is then excluded from
  later calls on the same router with `FilterReason.AUTH_DISABLED_THIS_RUN`.
- **Bad request (HTTP 400/422, or an explicit Responses invalid-input code)** --
  stop immediately. A malformed request is unlikely to fare better on another
  provider, and trying more would only bury the real cause under unrelated
  failures.
- **Bad `operation`/`arguments`** -- stop immediately. A `CallVariant.operation`
  that doesn't resolve on the provider's SDK client, or `arguments` that don't
  match its signature, is a caller/config mistake -- every provider sharing that
  protocol would fail the exact same way, so the router surfaces it rather than
  masking it under more failures.

Bad requests, invalid operations/arguments, and a missing required SDK stop the
entire call even when another protocol variant is available. This global
fail-fast behavior is intentional: a misconfigured preferred path should remain
visible instead of silently being replaced by a secondary protocol. These
failures never bench the provider. Retryable timeout, connection, auth,
rate-limit, 5xx, and unknown failures still fall back across protocols, using
each provider's matching operation, arguments, and model.

If every provider actually tried fails (or an unrecoverable failure stops the
run early), `invoke()` raises `RouterExhaustedError`, whose message enumerates
each attempted provider with its own real, distinct failure; the structured
attempts (each with its unwrapped error) stay on `error.attempts`.

Round robin plus fallback is the default with no configuration. To override the
selection order, pass a `policy` to the constructor:

```python
from nygen_router import ProviderRouter, RoundRobinPolicy

router = ProviderRouter(
    providers=[...], metrics_scope="my-application:production", policy=RoundRobinPolicy()
)
```

## Fixed provider preference (`StickyRoutingPolicy`)

`StickyRoutingPolicy` is an opt-in wrapper for applications that want selected
providers tried first in a fixed order. It is enabled only through the existing
`policy=` constructor seam; the default router and every other policy remain
unchanged.

```python
from nygen_router import ProviderRouter, ScoreBasedPolicy, StickyRoutingPolicy

router = ProviderRouter(
    providers=providers,
    metrics_scope="my-app",
    policy=StickyRoutingPolicy(
        sticky_provider_ids=["provider-a", "provider-b"],
        fallback_policy=ScoreBasedPolicy(),
    ),
)
```

The compact form gives the non-sticky remainder a fresh round-robin policy:

```python
router = ProviderRouter(
    providers=providers,
    metrics_scope="my-app",
    policy=StickyRoutingPolicy(sticky_provider_ids=["provider-a"]),
)
```

The constructor accepts a non-empty `list[str]`. Values are trimmed and copied;
non-string, blank, duplicate-after-trimming, and router-unknown IDs raise
`ConfigError` before a provider call. These are canonical `provider_id` values,
never display `name` values. A known provider may still be disabled or unhealthy
at construction because runtime eligibility remains the hard filter's job.

For every invocation, eligible sticky providers lead in configured ID order.
The wrapped round-robin, score-based, or custom policy is called once with a
fresh list containing only the eligible non-sticky remainder. Its ordering,
intentional omissions, and duplicates are preserved, but it cannot introduce a
sticky, disabled, unhealthy, unknown, or otherwise filtered provider. This
keeps the attempt order structurally bounded by the router's hard filters.

Without a retry policy, retryable failures move directly through the fixed
sticky prefix and then the wrapped tail. An independently configured
`retry_policy=` may first replay the first eligible sticky provider; sticky
routing itself never implies that behavior. Existing bad-request,
invalid-operation/arguments, and missing-SDK paths remain globally fail-fast.
Health benches remove preferred providers from later calls until normal health
rules make them eligible again. Streaming uses the same precomputed order:
`RESTART` continues through its tail, while `RAISE` and STOP categories do not.
A successful fallback never changes the next call's fixed preference, and raw
responses and chunks remain untouched.

Despite its name, this policy does not learn session or conversation affinity.
It stores no affinity key, outcome history, TTL, clock, persistence, cleanup,
reset, or per-call override. The preference is router-wide; create separate
router/policy instances—or a custom policy—for different users or workflows.
Separate policy instances per router are recommended because a wrapped policy
such as round robin may itself be stateful. Fixed preference may improve the
chance of provider-local prompt-cache reuse or concentrate traffic toward an
account tier, but it guarantees neither. Callers still own strict affinity for
provider-owned response IDs and state because health filtering and fallback can
choose another provider.

PR19 owns dedicated sticky-selection logging and PR20 owns optional
observability hooks. Same-provider retry composes independently through the
separate `retry_policy=` seam below; sticky routing never enables it.

## Optional same-provider retry

Same-provider retry is opt-in. Omitting `retry_policy`, or passing `None`,
preserves the default behavior: one base attempt for each provider occurrence in
the already-computed order, with normal cross-provider fallback. Provider SDK
retries stay disabled so router-controlled attempts remain visible.

```python
from nygen_router import ProviderRouter, SameProviderRetryPolicy

router = ProviderRouter(
    providers=providers,
    metrics_scope="my-application:production",
    retry_policy=SameProviderRetryPolicy(),
)
```

The default `max_attempts=3` means three **total physical attempts** for the
targeted provider, including its initial ordered attempt—at most two additional
retries, never three. The effective maximum is eight. A larger configured value
is clamped to eight and emits exactly one caller-facing `UserWarning` naming the
requested and effective values; values below two, booleans, and non-integers
raise `ConfigError`.

### Targeting modes

`FIRST` is the default and gives one retry cycle only to the provider at index
zero of the provider-ordering result. With score-based routing that is the
highest-ranked provider; with sticky routing it is the first eligible sticky
provider. Fallback providers still receive their ordinary base attempts.

```python
from nygen_router import RetryProviderScope, SameProviderRetryPolicy

retry_first = SameProviderRetryPolicy()
retry_all = SameProviderRetryPolicy(provider_scope=RetryProviderScope.ALL)
retry_selected = SameProviderRetryPolicy(
    provider_scope=RetryProviderScope.SELECTED,
    provider_ids=["provider-a-production", "provider-b-production"],
)
```

`ALL` gives one bounded cycle to the first reached occurrence of every distinct
eligible provider ID. `SELECTED` does so only for reached configured canonical
IDs. Selected IDs use an actual non-empty `list[str]`, are trimmed and copied,
and reject blanks, non-strings, duplicates, and IDs unknown to the router.
Filtered providers and providers omitted by a custom ordering policy are never
introduced. Custom ordering duplicates remain deliberate base attempts, but do
not multiply retry cycles for the same ID. The provider-ordering policy is still
called exactly once.

The built-in retries exactly these transient candidates:

- timeout, including HTTP 408 and typed Responses timeout codes;
- connection failure; and
- server error, including HTTP 5xx and typed Responses server-error codes.

These categories are candidates, not an idempotency guarantee. Unknown and
stream-interrupted failures are not retried by the built-in. Bad request and
invalid operation remain global fail-fast errors. Authentication and rate-limit
failures bench and fall back without same-provider retry. No retry policy,
including a custom one, can override those gates, a newly started health bench,
the effective attempt ceiling, or the opened-stream boundary. A trusted custom
policy may choose a pre-open unknown failure and receives a frozen
`RetryContext`; its decision must be exactly `bool`.

Every physical attempt is a normal health and metrics observation. Failures
increment health independently, successful retry resets counted health through
the normal success transition, and reaching `HealthConfig.failure_threshold`
immediately ends remaining retries for that provider. That health threshold is
a circuit breaker across attempts, not a retry budget. Metrics and scoring see
each failure and success as separate real events, and `RouterExhaustedError`
retains repeated provider IDs plus exact error objects in physical order. No
retry metadata is added to responses, events, stores, schemas, aggregation, or
scores.

Streaming retry applies only when `adapter.invoke()` fails before returning a
`NormalizedStream`. Such attempts record `stream_opened=False`, NULL TTFT, and
their measured total duration. Once a normalized stream opens—even if it later
yields zero chunks—PR27 performs no same-provider retry. `RouterStream` retains
its existing `RESTART` behavior on the already-computed provider tail or its
`RAISE` behavior, with chunks unchanged.

### Replay safety and lifecycle

**The router cannot determine whether a native request is safe to replay.** A
timeout or connection failure does not prove the provider failed to receive or
process the call. Retrying can duplicate provider work, tool side effects,
stored or background operations, charges, or any other non-idempotent behavior.
`CallVariant.arguments` remains opaque and is never inspected for tools,
idempotency keys, response IDs, or safety. Caller-supplied provider-native
idempotency mechanisms pass through unchanged, but the router neither creates
nor verifies them.

Selecting `retry_policy` is explicit router-wide acceptance of these risks.
There is no per-call override; use separate router instances or a carefully
scoped custom setup when different calls need different replay safety. Retry
counters live only inside one synchronous `invoke()` call. The built-in policy
stores frozen configuration and is safe to share across calls and routers; a
stateful custom policy owns its own thread safety. There is no sleep, delay,
backoff, jitter, `Retry-After`, total-duration budget, async execution,
persistent/distributed retry state, or background scheduler. PR19 and PR20
still own general retry logging and observability hooks; the maximum-clamp
warning is only a configuration signal.

## Provider health and cooldowns

Falling back on every call is wasteful if a provider is simply having a bad
hour: each call would pay its timeout again before moving on. So the router
tracks per-provider health and temporarily benches providers that are misbehaving.
A benched provider is excluded by the hard filter, so it costs nothing until its
bench expires.

This works with **zero configuration**. By default:

- A **rate limit (429)** benches that provider for **60 seconds**, immediately.
  A 429 is flow control rather than a broken provider, so it does not count
  toward the failure threshold below.
- **Three consecutive counted failures** (timeout, connection failure, server
  error, stream interruption, or unknown) bench that provider for **60
  seconds**. Only a success resets the count. With retry enabled, every physical
  failure contributes and a newly reached threshold ends the current retry
  cycle.
- An **auth failure (401/403)** benches that provider for the rest of the run,
  as it already did before.

Benches are always temporary and never silent -- every one is logged with the
provider's own verbatim error (see [Seeing benches](#seeing-benches)), readable
via `health_report()`, and clearable via `reset_health()`.

Because only a success resets the failure count, a provider that stays broken
costs **one failed probe per cooldown window** rather than three: when its bench
lapses it becomes eligible, and its next failure re-benches it at once. If every
provider is benched, `invoke()` raises `NoEligibleProvidersError` before making
any network call, and the message enumerates each provider's real root cause:

```
No eligible providers for this request: provider_a (id="provider-a-production"):
in cooldown (47.9s remaining) after 3 consecutive failures; last error:
Provider "provider_a" (id="provider-a-production") returned HTTP 404
Not Found for model 'gpt-4o-mini': The model does not exist; provider_b: in
cooldown (12.4s remaining) after rate limiting; last error: ...
```

### Tuning

Pass a `HealthConfig` to override any of the three knobs. The two cooldowns are
separate settings that happen to share a default, so they can diverge:

```python
from nygen_router import HealthConfig, ProviderRouter

router = ProviderRouter(
    providers=[...],
    metrics_scope="my-application:production",
    health=HealthConfig(
        rate_limit_cooldown_seconds=120.0,  # back off longer when rate limited
        failure_cooldown_seconds=30.0,
        failure_threshold=5,  # more tolerant of flaky providers
    ),
)
```

A plain dict works too, if you'd rather not import anything. It is validated
immediately, so a typo raises at construction instead of silently doing nothing:

```python
router = ProviderRouter(
    providers=[...],
    metrics_scope="my-application:production",
    health={"failure_threshold": 5},
)
```

### Inspecting health

`health_report()` returns one entry per configured provider, so you can see who
is benched and why before deciding to intervene. Healthy providers report clean
rather than going missing:

```python
for provider_id, health in router.health_report().items():
    print(provider_id, health.provider_name, health.cooldown_remaining_seconds)
```

Each entry is a frozen `ProviderHealthReport` with `auth_disabled`,
`consecutive_failures`, `cooldown_remaining_seconds` (`None` when not benched),
and `last_error`. Cooldowns are reported as remaining seconds, never as absolute
deadlines. The report is a copy: mutating it does not affect the router.

### Clearing a bench

When you have fixed the real cause -- upgraded a quota, corrected an API key --
waiting out the cooldown serves no purpose. `reset_health()` treats a provider as
brand new, clearing its cooldown, failure count, auth bench, and last error:

```python
router.reset_health("provider-a-production")  # one provider ID
router.reset_health()  # all providers
```

An unknown provider ID raises `ConfigError` rather than quietly doing nothing,
since a reset that silently no-ops is exactly the kind of failure this is meant
to prevent.

`reset_health()` never erases recorded metrics. The metrics store has no delete
path: every attempt that actually happened stays in the history forever. Reset
means "this provider may be tried again now", not "forget what happened".

### Seeing benches

Benches are reported on the standard library logger `nygen_router.router`:

- The first bench of an outage logs one **WARNING** naming the provider, the
  trigger, the bench duration, and the provider's verbatim error text -- so a
  typo'd `base_url` shows up as that provider's own 404 message.
- Repeat benches during that same outage drop to **DEBUG**, so one broken
  provider cannot flood your logs.
- The first success afterwards logs one **INFO** recovery line, and re-arms the
  warning: a later, separate outage warns again rather than being buried.

```python
import logging

logging.basicConfig(level=logging.INFO)  # WARNING benches + INFO recoveries
logging.getLogger("nygen_router.router").setLevel(logging.DEBUG)  # every bench
```

### Two caveats worth knowing

**Health lives on the router instance.** It is held in memory and dies with the
object: two routers have independent health, a process restart starts clean, and
an application that constructs a new `ProviderRouter` per request accumulates no
health signal at all. This protection is built for long-lived routers -- if that
is your setup, keep one router around rather than creating one per call.

**Cooldowns run on a monotonic clock**, so they are immune to wall-clock jumps
(NTP corrections, timezone changes). On most platforms that clock excludes time
the machine spends suspended, so suspending a laptop mid-cooldown stretches the
bench's wall-clock duration. That is harmless for the long-running workflows this
router targets.

## Multiple protocols in one call

When your configured providers expose the same logical model through different
API protocols, supply one `CallVariant` per protocol -- the router picks
whichever variant matches the provider it's about to try:

Both variants below use built-in adapters. If the selected Chat provider fails
with a retryable provider error, a Responses provider can be tried next, and
vice versa:

```python
response = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.REGULAR,
            arguments={"messages": [{"role": "user", "content": "Hello"}]},
        ),
        CallVariant(
            protocol=ApiProtocol.OPENAI_RESPONSES,
            operation="responses.create",
            call_type=CallType.REGULAR,
            arguments={"input": "Hello"},
        ),
    ]
)
```

Each protocol may appear at most once per call -- a second `CallVariant` for a
protocol already supplied raises `DuplicateCallVariantProtocolError`.
Every variant in one invocation must also declare the same `call_type`; mixed
regular and streaming response contracts raise `MixedCallTypeError` before any
provider is contacted.

## Streaming

Declare `CallType.STREAMING`, pass the provider's native `stream=True`
argument, and iterate. The chunks are the
provider SDK's own objects, in order, unbuffered. Chat yields
`ChatCompletionChunk` objects; Responses yields typed events such as
`response.output_text.delta`, as shown in the Responses section above.

```python
response = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.STREAMING,
            arguments={"messages": [{"role": "user", "content": "Hello"}], "stream": True},
        )
    ]
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

What you get on top of the raw SDK stream is that the router is still watching.
A stream that dies half-way through -- a dropped connection, a read timeout, or
a provider that simply stops sending without ever marking the response finished
-- falls back to the next occurrence in the same ranked order `invoke()` was
working through, instead of surfacing a raw SDK exception in your loop. The
remaining finite order is consumed without adding PR27 retry cycles; when none
is left you get `RouterExhaustedError` listing every physical attempt's real
reason.

A stream that ends without yielding even one chunk is also a failed attempt,
even if the provider marked it complete: it produced no usable response.
The router records a `ProviderStreamInterruptedError` and follows the same
configured stream-failure policy -- restart on the next provider by default,
or raise immediately under `StreamFailurePolicy.RAISE`.

### A restart means regenerating from scratch

Two generations cannot be spliced together. When the router restarts on a new
provider, **everything you have already accumulated from the dead provider must
be discarded** -- the new provider starts its answer over from the beginning.

That is never allowed to happen silently:

```python
from nygen_router import ProviderRouter, StreamRestart


def on_restart(restart: StreamRestart) -> None:
    print(
        f"discard {restart.chunks_yielded} chunk(s) from "
        f"{restart.failed_provider} ({restart.failed_provider_id})"
    )
    print(f"{restart.next_provider} ({restart.next_provider_id}) is regenerating")
    buffer.clear()


router = ProviderRouter(
    providers=[...], metrics_scope="my-application:production", on_restart=on_restart
)
```

If no callback is registered and chunks had already been yielded, the router
logs a warning instead. A restart that happens before any chunk was yielded
leaves you nothing to discard, so it fires neither. The number of restarts so
far is on the returned stream as `.restarts`.

To stop on any mid-stream failure rather than regenerate, set the policy:

```python
from nygen_router import ProviderRouter, StreamFailurePolicy

router = ProviderRouter(
    providers=[...],
    metrics_scope="my-application:production",
    stream_failure_policy=StreamFailurePolicy.RAISE,
)
```

`RAISE` re-raises the provider's own error, unchanged, after recording the
failed attempt. Both policies stop immediately on a malformed call (a 400, a
bad `operation`), across protocol variants, since hiding a broken preferred
path behind another variant would make configuration defects harder to find.

`call_type` is router metadata, not request-shaping configuration. The router
never inspects, inserts, or reconciles native `arguments["stream"]`; callers
must keep their declaration and native SDK arguments consistent. Deliberately
inconsistent calls remain caller-owned and fall outside the guaranteed
telemetry classification table.

### Stopping early

If you break out of the loop, close the stream -- or use it as a context
manager, which does it for you:

```python
with router.invoke([...]) as stream:
    for chunk in stream:
        if done_enough(chunk):
            break
```

Closing releases the provider's connection, never triggers a restart, and is
final: iterating a closed stream stops immediately, like a closed generator.
Closing also decides what gets recorded. If the provider had already marked the
response finished, the call is recorded as the success it was; if not, nothing
is recorded at all, because an outcome you declined to observe is not one the
router can honestly report. A bare `break` with no `close()` runs no router
code and records nothing.

### When truncation cannot be detected

A stream counts as successful only if it ended after the provider marked it
finished. For OpenAI-compatible Chat streams that marker is `finish_reason`;
for Responses streams it is `response.completed` or the explicitly served
terminal result `response.incomplete`. A recognized Responses stream that ends
without either terminal event is treated as interrupted. A declared
`response.failed` or `error` event is a provider failure, not stream data.

If a provider sends chunks in a shape the adapter does not recognize, there is
no marker to read, so the router will not claim a truncation it cannot
evidence: the stream counts as completed, and you get one warning per operation
saying truncation detection is unavailable for that stream shape. Fallback on
exceptions, metrics, and `close()` all still work normally for it.

### Custom adapters

Mid-stream fallback is opt-in. An adapter returning a raw SDK stream passes
straight through, exactly as before -- the router does not touch it. To opt in,
return a `NormalizedStream`: yield the SDK's chunks unchanged from `__next__`,
raise only router errors from it, and report `completed` and `usage`.

## Metrics persistence

Every provider attempt is recorded as one scoped `MetricsEvent`. Its scoring
identity is `metrics_scope + provider_id + model + protocol + call_type`;
`provider_name` is display metadata only. Excluded providers are not recorded.
The router always writes its required `metrics_scope`.

`call_type` records what the caller declared. `stream_opened` records what the
router observed: `None` for regular calls, `False` for a streaming attempt that
failed before returning a `NormalizedStream`, and `True` after one opened.
Streaming outcomes are written when completion or failure is known.
`latency_ms` is full-response latency for regular successes and TTFT for
streaming attempts; it remains `NULL` when no first chunk arrived. Failed
attempts never enter latency averages. `total_duration_ms` spans streaming
completion or failure, including a failure before opening.

PR13 added component-specific schema administration; shipped PR30 now creates a
fresh absent DuckDB or SQLite path transactionally at `metrics = 2`, with the
complete `provider_attempts` table, `nygen_router_schema_versions`, and every
backend-required project index. Later components such as durable health retain
their own row and revision stream. Reopening a current database validates
column names/order, logical types, nullability, defaults, primary-key identity,
metadata shape, metrics version, and required indexes without DDL.

SQLite v2 requires exactly one
`(provider_id, model, protocol, call_type, timestamp)` score-query index. Both
current- and all-scope benchmark plans used it. DuckDB v2 deliberately requires
no score-query ART index because measured plans used sequential scans with and
without the tested candidates.

The exact unversioned PR29 `provider_attempts` schema is still recognized as an
implicit version-1 baseline, but normal PR30 runtime rejects it unchanged, as
it does an explicitly versioned v1 target. PR30 provides no v1-to-v2 migration.
Stop all writers, then manually archive/delete a disposable old target or
configure an absent path. Every existing path—including an empty database, a
missing/partial/extra/reordered table, malformed metadata, a missing metrics
row, a malformed required index, v1/implicit v1, or a newer revision—is
inspected read-only and left untouched. Direct store calls raise an actionable
schema-mismatch error; router writes and score-history reads retain their
best-effort degradation. Runtime code never initializes, alters, stamps,
reindexes, backfills, renames, deletes, replaces, redirects, copies, or switches
an existing target.

PR11's request-size buckets are descoped because estimating size would require
the router to interpret opaque native arguments; the formerly reserved
`request_size_bucket` column has been removed from the schema. Use separate
`metrics_scope` values when materially different workloads need separate
routing history.

### Inspecting, creating, and migrating local databases

Schema administration is separate from `MetricsStore`, `ProviderRouter`, and
routing policies. The frozen typed Python API accepts only the explicit
`LocalBackend.DUCKDB` or `LocalBackend.SQLITE` selector:

```python
from nygen_router import (
    LocalBackend,
    create_database,
    inspect_database,
    migrate_database,
)

state = inspect_database(LocalBackend.SQLITE, "metrics.sqlite")
created = create_database(LocalBackend.DUCKDB)  # stable default path
migrated = migrate_database(
    LocalBackend.SQLITE,
    "metrics.sqlite",
    backup_path="metrics.before-migration.sqlite",
)
```

`inspect_database` opens an existing engine read-only and creates neither a
missing file nor its parent directory. Its typed result reports the resolved
path, default-path status, metadata state, every valid component revision,
compatibility, detected schema state, and next safe action.

`create_database` means “create a completely new current database.” It
atomically refuses every existing filesystem target, including empty,
incompatible, arbitrary, or current files. There is no `force`, overwrite,
delete, replace, archive, or alternate-name behavior. If a target is occupied,
inspect it; migrate it only when supported; manually move/archive it while the
application is stopped; or choose a different path and configure that concrete
store.

`migrate_database` is an administrator-triggered offline operation. Stop the
application, every router, and every other database writer first. It acquires
the backend's exclusive transaction/lock, builds the complete consecutive
route before writing, applies each step transactionally, updates its component
version in the same transaction after the corresponding step, rolls back on
failure, and is idempotent once current. There is currently no executable
migration: versioned v1 and implicit v1 are refused because PR30 deliberately
ships no route to v2. No arbitrary or experimental schema is guessed. Any
future optional explicitly named backup uses the engine's safe database-copy
operation, must target an absent path, is validated against the pre-migration
source before any source change, and remains available if later migration
fails. Backups are never automatic.

The same implementation is exposed by the standard-library CLI:

```sh
nygen-router storage inspect --backend duckdb --default
nygen-router storage inspect --backend sqlite --path ./metrics.sqlite
nygen-router storage create --backend duckdb --path ./metrics.duckdb
nygen-router storage migrate --backend sqlite --path ./metrics.sqlite \
  --backup ./metrics.before-migration.sqlite
```

DuckDB commands require the existing `duckdb` extra and fail with a concise
installation instruction when it is absent. CLI help and every SQLite command
remain available without DuckDB. Successful information goes to stdout; safe,
actionable failures go to stderr with stable nonzero exit statuses and no
traceback for ordinary administration errors.

Only DuckDB's standard `~/.nygen_router/metrics.duckdb` path is selected by an
otherwise unconfigured router. Creating any non-default file does not edit
source, environment variables, configuration, or a running router and does not
make that router discover it. Pass the concrete path explicitly:

```python
from nygen_router import DuckDBMetricsStore, ProviderRouter, SQLiteMetricsStore

metrics_store = DuckDBMetricsStore("/chosen/path/metrics.duckdb")
# or
metrics_store = SQLiteMetricsStore("/chosen/path/metrics.sqlite")

router = ProviderRouter(..., metrics_store=metrics_store)
```

`metrics_store` is a `ProviderRouter` constructor parameter with three forms:

```python
from nygen_router import DuckDBMetricsStore, ProviderRouter, SQLiteMetricsStore

# 1. Default: not passed at all -- a DuckDBMetricsStore at ~/.nygen_router/metrics.duckdb
router = ProviderRouter(providers=[...], metrics_scope="my-application:production")

# 2. Any MetricsStore implementation, e.g. the bundled SQLite backend
router = ProviderRouter(
    providers=[...],
    metrics_scope="my-application:production",
    metrics_store=SQLiteMetricsStore("metrics.sqlite"),
)

# 3. Disable persistence entirely
router = ProviderRouter(
    providers=[...], metrics_scope="my-application:production", metrics_store=None
)
```

`DuckDBMetricsStore` is the default: an embedded, no-server-to-run database,
requiring `pip install "nygen-router[duckdb]"`. Without that extra installed,
`ProviderRouter` still constructs (logging one warning) and `invoke()` still
works -- the write attempt fails with an `ImportError`, which is treated like
any other storage failure: the successful provider response is still
returned. **DuckDB is single-process**: it allows only one writing process
per file. If several local processes need to share one store, use
`SQLiteMetricsStore(path)` instead, which uses Python's stdlib `sqlite3` (no
extra install) and handles cross-process file locking natively. For shared,
multi-machine routing history, a Postgres/Supabase-backed store remains planned
in [`../Projectplan/NewProjectPlan.md`](../Projectplan/NewProjectPlan.md)
(PR25 and PR14). It will use the standard PostgreSQL protocol, optional lazy
dependencies, explicit deployment migrations, least-privilege runtime
connections, credential-safe errors, bounded connect/statement/pool timeouts,
explicit TLS and pooling mode, clear connection ownership, and idempotent
cleanup. It will not silently switch, mirror, replicate, or dual-write local
history.

Storage writes are best-effort and never replace or modify a provider response.
The router logs one short warning when a configured store first fails, continues
retrying later writes without repeating the warning, and logs one recovery message
if persistence starts working again. Full storage exceptions and tracebacks are
available at DEBUG logging level. Passing `metrics_store=None` intentionally disables
persistence and produces no warning.

### Bring your own backend

`MetricsStore` is a mandatory three-method `typing.Protocol`:

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

Implement all three methods against any SQL-compatible (or other) backend and
pass an instance as `metrics_store=...`—no router code changes are needed.
Router construction rejects legacy two-method objects and missing/non-callable
methods before any provider call. `query_recent` must retain raw chronological
event/filter semantics. `query_score_aggregates` must make one bounded backend
operation, match the exact scope/provider-ID/model/protocol/call-type/time
partition, implement flat and exponential weighting from the supplied one
reference time, and return exactly one validated `ScoreAggregate` for every
distinct requested provider, including explicit zeros for genuine empty
history. It returns intermediate totals, not `ProviderStats` or final scores.
Raise real backend errors; `ScoreBasedPolicy` owns graceful baseline fallback
and never retries through raw history.

To check your implementation against the same conformance suite the bundled
backends run, point `tests/test_metrics_store.py`'s parametrized `store`
fixture at a factory for your backend and add aggregate-equivalence/cardinality
coverage matching `tests/test_pr30_storage_source.py`.

PR30 added only bounded scoring aggregation. Dashboard/reporting queries belong
to PR28; PostgreSQL/Supabase remains PR14. No retention, health persistence,
cross-backend copy, rollup/cache, buffering, async behavior, or
`request_size_bucket` behavior was added.

## Concurrency and lifecycle

PR31 gives one router a defined concurrency contract and a defined end of
life. The matrix below is the whole promise; anything not listed as supported
is not supported.

Supported:

- **One `ProviderRouter` shared by multiple threads in one process.** Health
  reads and writes, policy ordering, adapter reuse, and metrics recording are
  serialized behind one router lock, so no attempt, bench, or rotation step is
  lost to a race.
- **Provider network calls running fully concurrently.** No lock is ever held
  across an adapter invocation or while a stream is being consumed; only the
  short bookkeeping sections at the edges of a call serialize. Writes remain
  eager and synchronous -- each attempt still pays one small local write.
- **`DuckDBMetricsStore` and `SQLiteMetricsStore` from multiple threads in one
  process.** Each store serializes every database operation (lazy connection
  creation, reads, writes, close) behind one per-instance lock. When the
  router lock and a store lock are both needed, the router's is always taken
  first; nothing takes them in the other order.
- **Several processes sharing one metrics database via `SQLiteMetricsStore`.**
  SQLite handles cross-process file locking natively.
- **`close()` and `with ProviderRouter(...) as router:`.** Close is idempotent
  and terminal, and closes only the store the router itself created (the
  unset-default DuckDB store). A caller-provided `metrics_store` is the
  caller's to close.
- **Custom policies without their own locks.** The router always calls
  `policy.order()` while holding its lock, so per-router mutable policy state
  is serialized automatically. The flip side: `order()` must not call back
  into the router, or it deadlocks on that lock.

Not supported:

- **Multiple processes writing one DuckDB file.** DuckDB allows one writing
  process per file; use `SQLiteMetricsStore` for a shared local file, or
  PostgreSQL (PR14) for shared organizational state.
- **Sharing health state across processes.** Health is in-memory by design
  and lives and dies with the router instance. Durable local health is PR25;
  shared organizational state is PR14.
- **One stream consumed by more than one thread.** A `RouterStream` is
  single-consumer: hand it to exactly one thread.
- **Using the router after `close()`.** `invoke()` raises `RouterClosedError`.
  `health_report()` and `reset_health()` keep working on the in-memory state.
- **Buffered/batched writes and native async.** Eager per-attempt writes
  remain; buffering is PR32 and native async is PR33.

The shutdown rule: finish calls and fully drain or close streams *before*
closing the router, and nothing is ever lost. Close never interrupts in-flight
work -- calls and streams complete normally -- but bookkeeping that reaches the
metrics path after close is dropped rather than written, precisely so a late
write cannot silently reopen (and re-lock) the database file close just
released. A stream finishing after close therefore loses exactly its one
metrics row, never its content; the drop is counted and logged at DEBUG, and
the in-memory health update still lands.

## Metrics aggregation

`aggregate_stats` remains the public, pure raw-history reference that turns
recorded attempts into one `ProviderStats` per provider. It is useful for
direct analysis and is the semantic oracle used to verify database aggregation:

```python
from datetime import UTC, datetime, timedelta

from nygen_router import CallType, aggregate_stats

store = SQLiteMetricsStore("metrics.sqlite")
events = store.query_recent(
    since=datetime.now(UTC) - timedelta(hours=1),
    metrics_scope=router.metrics_scope,
)

stats = aggregate_stats(events, router.providers, CallType.REGULAR)
print(stats["provider-a-production"].regular_success_rate)  # e.g. 0.95
```

It is query-only: the direct caller chooses the raw time/scope window, then
aggregation requires exact provider ID, model, protocol, and invocation call
type matches. Every configured provider gets an entry keyed by `provider_id`,
including one with no history at all—its counts are `0` and its rates and
averages are `None`.

`ScoreBasedPolicy` does not call this raw path. It always issues exactly one
`query_score_aggregates` request for all distinct eligible provider partitions,
and DuckDB/SQLite automatically execute one backend-specific parameterized SQL
query. There is no aggregation option and no `query_recent` fallback. The
database returns only:

- `attempt_weight` and `success_weight`;
- `successful_latency_weight` and
  `successful_latency_total_ms`;
- exact unweighted `recent_error_count`, `rate_limit_count`, and
  `timeout_count`.

Shared Python derives success rates and successful latency/TTFT averages,
constructs the selected regular or streaming `ProviderStats` bucket using the
current provider name, and runs the unchanged `calculate_provider_score`.
Final success quality, speed quality, and total score never move into SQL.

Every database result must contain exactly one row per distinct requested
provider ID. An explicit all-zero row means genuine empty history: the provider
receives the established optimistic-start score and later evidence
self-corrects it. A missing, duplicate, unexpected, negative, inconsistent,
NaN/infinite, or malformed row invalidates the complete read. Missing data is
never filled with optimistic zeros, because that could make a failure-only
provider repeatedly look new.

**Regular and streaming calls are counted separately** -- `regular_*` versus
`streaming_*` -- and never blended. They are not the same measurement: a
regular call's latency is the time to a complete response, a streaming call's
is the time to its first chunk, and the two are decided successful at
different moments. One combined number would hide precisely the weakness a
streaming-heavy workflow needs to see. A provider with populated regular
figures and empty streaming ones (or the reverse) is normal.

Averages are computed over successful attempts only, so a provider that fails
in 5ms never looks faster than one that answers in 500ms. `recent_error_count`,
`rate_limit_count`, and `timeout_count` are exact tallies for the selected
partition, for diagnostics. A successful event with NULL latency adds success
evidence but no latency weight or total; failed latency never contributes.

## Score calculation

`calculate_provider_score(stats, weights, call_type=CallType.REGULAR)` turns
one provider's aggregated observations into a comparable score between 0 and
1. Pass `CallType.STREAMING` to select streaming success and TTFT. The returned
`ProviderScore` keeps `success_quality` and `speed_quality` alongside `total`,
so the result remains explainable.

`ScoreWeights` controls the calculation:

| Setting | Default | Valid range | What it controls |
| --- | ---: | --- | --- |
| `success_weight` | `1.0` | `>= 0` | Relative influence of observed success rate. |
| `speed_weight` | `1.0` | `>= 0` | Relative influence of latency. |
| `regular_latency_reference_ms` | `2000.0` | `> 0` | Regular latency that maps to a raw speed quality of `0.5`. |
| `streaming_ttft_reference_ms` | `500.0` | `> 0` | Streaming TTFT that maps to a raw speed quality of `0.5`. |
| `optimistic_start` | `0.75` | `0` to `1` | Initial quality assumed before real evidence accumulates. |
| `optimistic_start_pretend_attempts` | `5.0` | `> 0` | Strength of that initial assumption, measured in pretend attempts. |

At least one of `success_weight` and `speed_weight` must be greater than zero.
The total is their weighted average, so the weights express relative
importance and do not need to add up to any particular value.

Success and speed are both blended toward `optimistic_start` as though the
provider began with `optimistic_start_pretend_attempts` imaginary observations.
A brand-new provider therefore starts at `0.75`, not zero; a small amount of
history moves it gently, while enough real attempts eventually dominate the
prior. This always-on optimistic start is how new providers remain eligible
for exploration—there is no separate exploration-bonus switch.

Cost is deliberately not a scoring factor. Automatic pricing is outside the
router's core model, and manually configured cost remains the deferred,
optional work described in
[`../Projectplan/NewProjectPlan.md`](../Projectplan/NewProjectPlan.md) (PR6).
Any future implementation must use explicitly supplied usage and pricing rather
than inspect native call arguments; PR24's router-owned instrumentation is
descoped.

## Score-based routing

`ScoreBasedPolicy` ranks every eligible provider from recent observations and
hands the full best-first list to the router's existing fallback loop:

```python
from nygen_router import HistoryScope, ProviderRouter, ScoreBasedPolicy, ScoreWeights

policy = ScoreBasedPolicy(
    weights=ScoreWeights(success_weight=2.0, speed_weight=1.0),
    lookback_hours=336.0,
    history_scope=HistoryScope.CURRENT,
)
router = ProviderRouter(providers=[...], metrics_scope="my-application:production", policy=policy)
```

Its constructor settings are:

- `weights=None`: use the default `ScoreWeights`.
- `lookback_hours=336.0`: query the latest 336 hours, which is 14 days, and
  count every event in that window equally.
- `half_life_hours=None`: keep flat weighting over `lookback_hours`; a positive
  value replaces that window with the recency behavior described below.
- `history_scope=HistoryScope.CURRENT`: read only the router's configured
  scope. `HistoryScope.ALL` explicitly combines otherwise matching provider
  partitions across scopes; writes still use the router's current scope.
- `tie_break_policy=None`: use an internal `RoundRobinPolicy`.
- `now=...`: wall-clock seam for deterministic testing; normal applications
  use its UTC default.

The tie-break policy is applied first, then a stable score sort preserves that
order wherever scores are equal. With the default, equal-scoring providers
therefore rotate through round robin instead of one permanently winning every
tie. The policy captures `now` once, deduplicates the baseline only for
aggregate request construction, issues exactly one aggregate call, and stably
sorts every original baseline occurrence. The query matches the lower time
bound, optional exact scope, provider ID, model, protocol, and caller-declared
call type.

The same baseline order is returned unchanged when metrics are disabled, the
aggregate query raises, aggregate rows are invalid, or scores are equal. The
first unavailable-history event logs a warning and repeats log at DEBUG.
Failure never calls `query_recent`, never uses partial rows, and never prevents
the router from trying a provider.

The router puts the invocation's declared `CallType` in `RoutingContext`, and
`ScoreBasedPolicy` automatically scores that matching partition. There is no
independent `use_streaming` policy setting that can contradict the call.

### Recency weighting

`half_life_hours` optionally makes recent observations count more than older
ones. A half-life is the age at which an event carries half its original
weight. For example, with `half_life_hours=72`, an event from three days ago
counts half as much as one from right now; after six days it counts one
quarter as much.

The default is `half_life_hours=None`, which leaves recency weighting off and
preserves the flat `lookback_hours=336` behavior above. Setting a positive
half-life replaces `lookback_hours` entirely for that policy instance; the two
windows are never combined. The router queries the latest six half-lives and
the backend applies exponential decay within them using
`0.5 ** (age_hours / half_life_hours)`. Flat mode uses weight 1.0 for every
event in its lookback. Both modes use the single timezone-aware reference time
captured for the whole ordering; the database does not choose its own current
timestamp. Events older than the exponential boundary are ignored after their
influence has already fallen below about 1.6%.

```python
policy = ScoreBasedPolicy(half_life_hours=72.0)
```

### PR30 benchmark and schema rationale

Run the separate benchmark from this package directory; it is intentionally
not part of ordinary pytest:

```sh
.venv/bin/python benchmarks/pr30_score_aggregation.py --rows 60000 --repetitions 7
```

The validated run generated 60,000 logical event rows, requested 9 provider
partitions, returned exactly 9 aggregate rows, and timed 7 repetitions after a
warm-up. SQLite's one retained
`(provider_id, model, protocol, call_type, timestamp)` index served both plans:
medians were 1.240208 ms current-scope and 2.090667 ms all-scope, versus
43.191167/43.610417 ms unindexed. An optional second scope-leading index
reached 0.556125 ms for current scope, but was rejected because the write and
storage cost did not justify an index that helped only that plan.

DuckDB used sequential scans with no score index and with two candidate ART
indexes. No-index medians were 8.648875/9.006959 ms; two-index medians were
8.202791/9.003167 ms, with higher seed/storage cost. No decorative DuckDB
indexes were retained. These timings are evidence from one machine, not a
universal latency guarantee; the stable promises are one backend aggregate
call, bounded returned cardinality, semantic equivalence, and inspectable query
plans.

The one-time PR30 demo recreation discarded 2 rows from the default
`~/.nygen_router/metrics.duckdb` and 46 rows from
`../WorkflowTests/workflow_history.duckdb`, then left both empty and validated
at metrics v2 after smoke checks.
`../WorkflowTests/workflow_history.pre-pr29.duckdb` remained untouched. Normal
runtime has no equivalent reset or overwrite behavior.

PostgreSQL/Supabase remains PR14. Reporting remains PR28; buffered/batched
writes PR32; and native async routing/storage PR33. Baseline in-process thread
safety and router lifecycle shipped as PR31 -- see "Concurrency and
lifecycle". Rollups, caches, retention, and materialized scores are also
deferred.

### Stable provider identity and display names

Every `ProviderConfig` requires a nonblank, stable `provider_id`. IDs must be
unique within one router and are the canonical key for metrics, health,
attempts, exclusions, resets, scores, and provider-specific errors. `name`
remains required display metadata; duplicate names are valid, though distinct
names are easier to read. Diagnostics expose both whenever duplicates could be
ambiguous.

Use separate IDs for separate accounts, API-key quota domains, deployments,
gateways, or endpoints that should learn independently. API keys are never
stored, hashed, compared, or exposed. Changing only `name` preserves history;
changing ID, model, protocol, or declared call type selects a fresh partition.
Changing `base_url` alone does not—assign a new ID when it represents a
different failure domain.

## Errors

The router is deliberately transparent about failures -- no "peel the onion"
debugging. The contract:

- **One base type.** Every error the router raises derives from
  `NygenRouterError`, so `except NygenRouterError` catches all of them. Specific
  types remain available for granular handling.
- **The exact provider error is preserved.** For a non-2xx response,
  `ProviderHTTPError` carries the provider's verbatim `message`, plus
  `status_code`, `error_type`, `error_code`, the full `body`, and the raw
  `response`. The underlying `openai` SDK exception stays reachable via
  `__cause__` and `.original`.
- **The error type names the stage.** `ConfigError` / `MissingApiKeyError`
  (configuration), `ModelArgumentConflictError` / `DuplicateCallVariantProtocolError`
  (malformed call, before any provider is contacted),
  `UnsupportedProtocolError` (no adapter for a configured protocol),
  `ProviderSDKNotInstalledError` (missing optional dependency),
  `NoProvidersConfiguredError` (no providers configured),
  `NoEligibleProvidersError` (all providers filtered out before any call),
  `RouterExhaustedError` (every provider tried failed),
  `UnsupportedOperationError` / `InvalidOperationArgumentsError` (bad
  `operation`/`arguments`),
  `ProviderTimeoutError` / `ProviderConnectionError` / `ProviderError` (transport),
  `ProviderHTTPError` (HTTP status), `ProviderResponsesError` (a native
  `response.failed`/`error` result), `ProviderStreamInterruptedError` (a stream
  ended without the provider ever marking it finished). Provider-specific
  errors name the provider and model; aggregate errors enumerate the relevant
  providers. `ProviderResponsesError.event` and `.response` retain typed native
  objects; `.error_code`, `.message`, and `.param` retain provider fields.
- **Originals are chained, never re-wrapped.** Transport and SDK failures keep
  the exact `openai`/`httpx` exception type in the message and attach it as both
  `__cause__` and `.original`.

```python
from nygen_router import ProviderHTTPError, ProviderRouter

try:
    router.invoke([...])
except ProviderHTTPError as error:
    print(error)  # Provider 'provider_a' returned HTTP 429 Too Many Requests ...
    print(error.status_code)  # 429
    print(error.error_type)  # e.g. "rate_limit_exceeded"
    print(error.body)  # the provider's error payload
    raise error.__cause__  # the underlying openai SDK exception, if you want it
```

## Quality Checks

```sh
ruff format .
ruff check .
mypy src
pytest
coverage run -m pytest
coverage report
```
