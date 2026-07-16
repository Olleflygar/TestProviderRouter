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

Only the OpenAI Chat Completions protocol is implemented so far, dispatched via
the official `openai` Python SDK (used against any OpenAI-compatible `base_url`,
not just OpenAI itself). Every provider attempt is recorded as an observational
metrics event behind a swappable `MetricsStore` (DuckDB by default, SQLite as a
fully-supported alternative) -- see "Metrics persistence" below. Scoring, the
Responses API, and framework adapters are future PRs.

## Local Development

Requires **Python 3.12+**. Do not use a conda env on Python 3.10 for this package.

```sh
cd ProviderRouterPR1
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
from nygen_router import ApiProtocol, CallVariant, ProviderConfig, ProviderRouter

router = ProviderRouter(
    providers=[
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="my-model",
            base_url="https://api.provider-a.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        )
    ]
)

response = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
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

## Installing the openai SDK

The core package (`from nygen_router import ProviderRouter`) never requires any
provider SDK -- that import always works with just `pydantic` installed. The
`OPENAI_CHAT` protocol's adapter lazily imports `openai` only when it's actually
invoked, so install the matching extra to use it:

```sh
pip install "nygen-router[openai]"
```

```sh
export PROVIDER_A_API_KEY="your-key"
```

API keys can also be passed explicitly with `api_key`, but keys are never printed
or included in router errors.

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

Note: the router does not currently check whether a provider *declares* support
for what a call's `arguments` actually need (tool calls, streaming, JSON mode,
etc.) -- `ProviderConfig.capabilities` exists and can be set, but nothing reads
it yet. A provider that can't handle a given call today discovers that the same
way any other failure is discovered: the call fails and the router falls back to
the next eligible provider. Automatic capability-based pre-flight filtering,
driven from a call's own `arguments`, is a planned follow-up (see
`Projectplan/ProjectPlan.md`, PR 21).

If filtering removes every configured provider, `invoke()` raises
`NoEligibleProvidersError`, whose message enumerates each excluded provider with
its own specific reason rather than a single blended summary.

```python
try:
    response = router.invoke([...])
except NoEligibleProvidersError as error:
    print([(e.provider_name, e.reason) for e in error.exclusions])  # who was filtered, and why
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
- **Bad request (other 4xx, e.g. 400/422)** -- stop immediately. A malformed
  request is unlikely to fare better on another provider, and trying more would
  only bury the real cause under unrelated failures.
- **Bad `operation`/`arguments`** -- stop immediately. A `CallVariant.operation`
  that doesn't resolve on the provider's SDK client, or `arguments` that don't
  match its signature, is a caller/config mistake -- every provider sharing that
  protocol would fail the exact same way, so the router surfaces it rather than
  masking it under more failures.

If every provider actually tried fails (or an unrecoverable failure stops the
run early), `invoke()` raises `RouterExhaustedError`, whose message enumerates
each attempted provider with its own real, distinct failure; the structured
attempts (each with its unwrapped error) stay on `error.attempts`.

Round robin plus fallback is the default with no configuration. To override the
selection order, pass a `policy` to the constructor:

```python
from nygen_router import ProviderRouter, RoundRobinPolicy

router = ProviderRouter(providers=[...], policy=RoundRobinPolicy())
```

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
  error, or unknown) bench that provider for **60 seconds**. Only a success
  resets the count.
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
No eligible providers for this request: provider_a: in cooldown (47.9s remaining)
after 3 consecutive failures; last error: Provider 'provider_a' returned HTTP 404
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
    health=HealthConfig(
        rate_limit_cooldown_seconds=120.0,  # back off longer when rate limited
        failure_cooldown_seconds=30.0,
        failure_threshold=5,                # more tolerant of flaky providers
    ),
)
```

A plain dict works too, if you'd rather not import anything. It is validated
immediately, so a typo raises at construction instead of silently doing nothing:

```python
router = ProviderRouter(providers=[...], health={"failure_threshold": 5})
```

### Inspecting health

`health_report()` returns one entry per configured provider, so you can see who
is benched and why before deciding to intervene. Healthy providers report clean
rather than going missing:

```python
for name, health in router.health_report().items():
    print(name, health.cooldown_remaining_seconds, health.last_error)
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
router.reset_health("provider_a")  # one provider, eligible again on the next call
router.reset_health()              # all providers
```

An unknown provider name raises `ConfigError` rather than quietly doing nothing,
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

```python
response = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={"messages": [{"role": "user", "content": "Hello"}]},
        ),
        CallVariant(
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            operation="messages.create",
            arguments={"messages": [{"role": "user", "content": "Hello"}]},
        ),
    ]
)
```

Each protocol may appear at most once per call -- a second `CallVariant` for a
protocol already supplied raises `DuplicateCallVariantProtocolError`.

## Metrics persistence

Every provider attempt (success or failure) is recorded as one observational
`MetricsEvent` -- provider name, model, protocol, success, latency, and error
type -- so score-based routing (a later PR) has real history to work from.
Excluded providers are not recorded.

`metrics_store` is a `ProviderRouter` constructor parameter with three forms:

```python
from nygen_router import DuckDBMetricsStore, ProviderRouter, SQLiteMetricsStore

# 1. Default: not passed at all -- a DuckDBMetricsStore at ~/.nygen_router/metrics.duckdb
router = ProviderRouter(providers=[...])

# 2. Any MetricsStore implementation, e.g. the bundled SQLite backend
router = ProviderRouter(providers=[...], metrics_store=SQLiteMetricsStore("metrics.sqlite"))

# 3. Disable persistence entirely
router = ProviderRouter(providers=[...], metrics_store=None)
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
multi-machine routing history, a Postgres/Supabase-backed store is a planned
future backend.

Storage writes are best-effort and never replace or modify a provider response.
The router logs one short warning when a configured store first fails, continues
retrying later writes without repeating the warning, and logs one recovery message
if persistence starts working again. Full storage exceptions and tracebacks are
available at DEBUG logging level. Passing `metrics_store=None` intentionally disables
persistence and produces no warning.

### Bring your own backend

`MetricsStore` is a two-method `typing.Protocol`:

```python
class MetricsStore(Protocol):
    def record_attempt(self, event: MetricsEvent) -> None: ...
    def query_recent(
        self, *, since: datetime, provider_name: str | None = None, model: str | None = None
    ) -> list[MetricsEvent]: ...
```

Implement those two methods against any SQL-compatible (or other) backend and
pass an instance as `metrics_store=...` -- no router code changes needed. To
check your implementation against the same conformance suite the bundled
backends run, point `tests/test_metrics_store.py`'s parametrized `store`
fixture at a factory for your backend.

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
  (malformed call, before any provider is contacted), `ProviderSDKNotInstalledError`
  (missing optional dependency), `NoProvidersConfiguredError` (no providers
  configured), `NoEligibleProvidersError` (all providers filtered out before any
  call), `RouterExhaustedError` (every provider tried failed), `UnsupportedOperationError`
  / `InvalidOperationArgumentsError` (bad `operation`/`arguments`),
  `ProviderTimeoutError` / `ProviderConnectionError` / `ProviderError` (transport),
  `ProviderHTTPError` (HTTP status). Messages always name the provider and model.
- **Originals are chained, never re-wrapped.** Transport and SDK failures keep
  the exact `openai`/`httpx` exception type in the message and attach it as both
  `__cause__` and `.original`.

```python
from nygen_router import ProviderHTTPError, ProviderRouter

try:
    router.invoke([...])
except ProviderHTTPError as error:
    print(error)             # Provider 'provider_a' returned HTTP 429 Too Many Requests ...
    print(error.status_code) # 429
    print(error.error_type)  # e.g. "rate_limit_exceeded"
    print(error.body)        # the provider's error payload
    raise error.__cause__    # the underlying openai SDK exception, if you want it
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
