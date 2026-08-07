# Errors & health

## Errors

Every router error derives from `NygenRouterError`. Provider failures keep the
verbatim message and structured fields; originals stay on `__cause__` and
`.original`.

| Situation | Typical type |
|-----------|----------------|
| Bad config / unknown ID | `ConfigError`, `MissingApiKeyError` |
| Malformed call before contact | `ModelArgumentConflictError`, `DuplicateCallVariantProtocolError`, `MixedCallTypeError` |
| Nothing eligible | `NoEligibleProvidersError` (lists each exclusion) |
| Every tried provider failed | `RouterExhaustedError` (lists real attempts) |
| HTTP non-2xx | `ProviderHTTPError` (`status_code`, `body`, …) |
| Responses `failed` / `error` | `ProviderResponsesError` |
| Stream died unfinished | `ProviderStreamInterruptedError` |
| Bad `operation` / `arguments` | `UnsupportedOperationError`, `InvalidOperationArgumentsError` |
| Missing optional SDK | `ProviderSDKNotInstalledError` |

```python
from llm_provider_router import ProviderHTTPError

try:
    router.invoke([...])
except ProviderHTTPError as error:
    print(error.status_code, error.message)
    print(error.body)
```

### What happens next

- **Timeout, connection, 5xx, unknown** → fall back (and count toward health).
- **429** → fall back; immediate cooldown (not a failure-threshold strike).
- **401/403** → fall back; auth-bench for the rest of this router lifetime.
- **400/422 / invalid input / bad operation / missing SDK** → stop the whole
  call immediately (global fail-fast), no bench.

## Health and cooldowns

Falling back on every call is wasteful if a provider is having a bad hour. The
router benches misbehaving providers so eligibility skips them until the bench
expires.

Defaults (zero config):

- **429** → 60s cooldown immediately.
- **3 consecutive counted failures** (timeout, connection, 5xx, stream
  interrupt, unknown) → 60s cooldown. Only a success resets the count.
- **401/403** → benched for the rest of the run.

With retry enabled, every physical failure counts; hitting the threshold ends
that provider's remaining retry cycle.

### Tuning

```python
from llm_provider_router import HealthConfig

router = ProviderRouter(
    ...,
    health=HealthConfig(
        rate_limit_cooldown_seconds=120.0,
        failure_cooldown_seconds=30.0,
        failure_threshold=5,
    ),
)
```

A plain dict works too and is validated at construction.

### Inspect and reset

```python
for provider_id, health in router.health_report().items():
    print(provider_id, health.cooldown_remaining_seconds, health.last_error)

router.reset_health("provider-a-production")  # one ID
router.reset_health()                         # all
```

Unknown IDs raise `ConfigError`. Reset means “may be tried again now” — it
never deletes metrics history.

### Logging

Benches log on `llm_provider_router.router`: first bench of an outage at
**WARNING** (with the provider's verbatim error), repeats at **DEBUG**, first
recovery at **INFO**.

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("llm_provider_router.router").setLevel(logging.DEBUG)
```

### Caveats

- Health is **in-memory on the router instance**. A new router per request
  accumulates no health signal — keep long-lived routers.
- Cooldowns use a monotonic clock (immune to wall-clock jumps; suspend can
  stretch wall time, which is usually fine).

See [Concurrency](./concurrency.md) for cross-process limits (health is not
shared across processes).
