# Policies

Two separate knobs:

- **Routing policies** (`policy=`) decide the *order* of eligible providers.
- **Retry policy** (`retry_policy=`) optionally re-attempts the *same* provider
  before moving on.

They compose, but they are not the same abstraction. Omitting either keeps the
defaults: round-robin ordering, and no same-provider retry.

---

## Routing policies

Routing runs only on providers that already passed eligibility. A policy
implements `order(eligible, context)` and returns the attempt order. The
router's fallback loop then walks that list.

### Round robin (default)

```python
from llm_provider_router import ProviderRouter, RoundRobinPolicy

router = ProviderRouter(
    providers=[...],
    metrics_scope="my-app",
    policy=RoundRobinPolicy(),  # same as the default
)
```

Eligible providers rotate across successive `invoke()` calls. Rotation is
in-process only — it does not survive restarts.

### Sticky preference

Try specific providers first, in a fixed ID order. Everything else is handed to
a wrapped policy (fresh round-robin if you omit `fallback_policy`):

```python
from llm_provider_router import ScoreBasedPolicy, StickyRoutingPolicy

policy = StickyRoutingPolicy(
    sticky_provider_ids=["provider-a", "provider-b"],
    fallback_policy=ScoreBasedPolicy(),
)
```

Use canonical `provider_id` values, not display names. Eligibility still wins:
a sticky provider that is disabled, keyless, or benched is simply absent.

Despite the name, this does **not** learn session affinity. Preference is
router-wide and fixed until you change config. Successful fallback never
rewrites the next call's order. Sticky routing never turns on retry — pass
`retry_policy=` separately if you want that.

### Score-based

Rank eligible providers from recent metrics (success + latency/TTFT), then hand
the best-first list to the same fallback loop:

```python
from llm_provider_router import HistoryScope, ScoreBasedPolicy, ScoreWeights

policy = ScoreBasedPolicy(
    weights=ScoreWeights(success_weight=2.0, speed_weight=1.0),
    lookback_hours=336.0,          # 14 days, flat weights
    history_scope=HistoryScope.CURRENT,
)
```

Useful knobs:

| Setting | Default | Notes |
|---------|---------|--------|
| `lookback_hours` | `336` | Flat window when `half_life_hours` is unset |
| `half_life_hours` | `None` | Set e.g. `72` for exponential recency; replaces lookback |
| `history_scope` | `CURRENT` | `ALL` combines matching partitions across scopes |
| `tie_break_policy` | round-robin | Breaks equal scores |

New providers get an optimistic start (~0.75), so they still get tried. Regular
and streaming history never blend. If metrics are off or the aggregate read
fails, the tie-break order is used unchanged — scoring never blocks the call.

Use separate `metrics_scope` values when workloads should not share history.

---

## Retry policy

Same-provider retry is **opt-in** and independent of `policy=`. Omit
`retry_policy` (or pass `None`) for one base attempt per ordered provider, then
normal cross-provider fallback. Provider SDK retries stay disabled so router
attempts remain visible.

```python
from llm_provider_router import RetryProviderScope, SameProviderRetryPolicy

router = ProviderRouter(
    providers=providers,
    metrics_scope="my-app",
    retry_policy=SameProviderRetryPolicy(),  # max_attempts=3 by default
)
```

`max_attempts` is total physical attempts for the targeted cycle, including the
first try (default 3 → at most two retries). Values above 8 clamp to 8 with one
`UserWarning`.

### Who gets a retry cycle

| Scope | Behavior |
|-------|----------|
| `FIRST` (default) | Only index 0 of the ordered list |
| `ALL` | First reached occurrence of every distinct eligible ID |
| `SELECTED` | Only listed canonical IDs that are actually reached |

```python
SameProviderRetryPolicy(provider_scope=RetryProviderScope.ALL)
SameProviderRetryPolicy(
    provider_scope=RetryProviderScope.SELECTED,
    provider_ids=["provider-a-production"],
)
```

Built-in retries cover timeout, connection failure, and server error (5xx).
Auth and rate limits bench then fall back without same-provider retry. Bad
request / invalid operation stay global fail-fast. Streaming retry is
**pre-open only** — once a stream is open, mid-stream recovery uses streaming
restart on the next provider, not PR27 retry.

Every physical attempt is a normal metrics and health observation.

### Replay safety

**The router cannot tell whether a native request is safe to replay.** A
timeout does not prove the provider never processed the call. Retrying can
duplicate work, tool side effects, stored/background operations, or charges.
`arguments` stay opaque; caller-supplied idempotency headers pass through but
are neither created nor verified.

Choosing `retry_policy=` is an explicit, router-wide acceptance of that risk.
There is no per-call override — use separate router instances when different
calls need different replay rules.

## How they fit together

1. Eligibility filters the configured list.
2. `policy.order(...)` ranks what's left (sticky prefix + wrapped tail, scores,
   or round-robin).
3. For each occurrence, `retry_policy` may run a bounded same-provider cycle.
4. Then fallback continues to the next ordered provider.

See also [Errors & health](./errors-and-health.md) and the
[package README](../ProviderRouter/README.md) for full classification tables.
