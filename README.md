# Nygen Router

Nygen Router routes one native LLM call across multiple providers that can serve
the same model. Your application keeps the provider SDK's request shape and
response type; the router selects the provider, falls back when appropriate,
and records runtime observations for later routing decisions.

## Onboarding: route the model call

Without Nygen Router, an application sends every request to one configured
provider.

### Before: calling one provider directly

```python
import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ["PROVIDER_A_API_KEY"],
    base_url="https://provider-a.example.com/v1",
)

response = client.chat.completions.create(
    model="provider-a/model-name",
    messages=[{"role": "user", "content": "Write a short product description."}],
)

print(response.choices[0].message.content)
```

### After: calling through Nygen Router

```python
response = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.REGULAR,
            arguments={
                "messages": [
                    {"role": "user", "content": "Write a short product description."}
                ]
            },
        )
    ]
)

print(response.choices[0].message.content)
```

The call remains native to the provider SDK. The difference is that Nygen Router
chooses an eligible provider and injects that provider's configured model. The
winning provider's original SDK response is returned unchanged.

## Minimal router setup

Configure equivalent models available through two OpenAI-compatible provider
endpoints. `ScoreBasedPolicy` ranks them using recent success and latency
observations stored in DuckDB. With no history, its round-robin tie breaker gives
each provider a chance to collect observations.

```python
from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    DuckDBMetricsStore,
    ProviderConfig,
    ProviderRouter,
    SameProviderRetryPolicy,
    ScoreBasedPolicy,
    StickyRoutingPolicy,
)

metrics = DuckDBMetricsStore("router_metrics.duckdb")

router = ProviderRouter(
    providers=[
        ProviderConfig(
            provider_id="provider-a-production",
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="provider-a/model-name",
            base_url="https://provider-a.example.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        ),
        ProviderConfig(
            provider_id="provider-b-production",
            name="provider_b",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="provider-b/model-name",
            base_url="https://provider-b.example.com/v1",
            api_key_env="PROVIDER_B_API_KEY",
        ),
    ],
    metrics_scope="product-copy:production",
    policy=ScoreBasedPolicy(),
    metrics_store=metrics,
)

def call_router(prompt: str) -> str:
    response = router.invoke(
        [
            CallVariant(
                protocol=ApiProtocol.OPENAI_CHAT,
                operation="chat.completions.create",
                call_type=CallType.REGULAR,
                arguments={
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
            )
        ]
    )
    return response.choices[0].message.content or ""


print(call_router("Write a short product description."))
```

`model` is deliberately absent from `CallVariant.arguments`: the router inserts
the model configured for whichever provider it selects. API keys can also be
passed directly to `ProviderConfig`, but environment-variable names keep secrets
out of source code.

`provider_id` is the stable identity used for metrics, health, attempts, and
diagnostics; `name` is display metadata and may be duplicated. `metrics_scope`
partitions the shared history file by application/environment. Changing only a
display name preserves history, while changing model, protocol, call type, or
provider ID selects a different scoring partition.

## Local metrics database administration

Fresh DuckDB and SQLite databases use a component-versioned schema beginning
with `metrics = 1`. Normal router use creates that schema only when the selected
file path is absent. An existing database is validated read-only: the exact
unversioned PR29 schema remains usable without being stamped, while every
incompatible, malformed, or newer target is left untouched and metrics degrade
safely at the router boundary.

Use the separate administrator CLI to inspect, create, or explicitly migrate a
local target:

```sh
nygen-router storage inspect --backend duckdb --default
nygen-router storage create --backend sqlite --path ./router_metrics.sqlite
nygen-router storage migrate --backend sqlite --path ./router_metrics.sqlite \
  --backup ./router_metrics.before-pr13.sqlite
```

`inspect` never creates or changes a file. `create` accepts only an absent
target and has no force/delete/replace mode. `migrate` is offline: stop the
application, routers, and every other writer first. It accepts only complete
known migration routes, runs them transactionally, and stamps the exact
implicit version-1 baseline without inventing a historical transform. A backup
is optional, explicitly named, engine-safe, validated before migration, and
never overwrites an existing destination.

The default router automatically reuses only
`~/.nygen_router/metrics.duckdb`. A database created at any other path is not
discovered automatically; configure it explicitly:

```python
from nygen_router import DuckDBMetricsStore, ProviderRouter, SQLiteMetricsStore

metrics_store = DuckDBMetricsStore("/chosen/path/metrics.duckdb")
# Or: metrics_store = SQLiteMetricsStore("/chosen/path/metrics.sqlite")
router = ProviderRouter(..., metrics_store=metrics_store)
```

PostgreSQL/Supabase remains a future PR14 backend using the standard PostgreSQL
protocol. PR30 owns storage-side scoring aggregates; PR28 owns reporting
queries. PR13 added none of those features and did not restore the removed
`request_size_bucket` metric.

## OpenAI Responses API

`OPENAI_RESPONSES` is also built in. Use the native Responses `input` field and
read the real SDK response, including conveniences such as `output_text`:

```python
responses_router = ProviderRouter(
    providers=[
        ProviderConfig(
            provider_id="provider-a-responses-production",
            name="provider_a_responses",
            protocol=ApiProtocol.OPENAI_RESPONSES,
            model="provider-a/model-name",
            base_url="https://provider-a.example.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        )
    ],
    metrics_scope="product-copy:production",
    metrics_store=None,
)

response = responses_router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_RESPONSES,
            operation="responses.create",
            call_type=CallType.REGULAR,
            arguments={"input": "Write a short product description."},
        )
    ]
)
print(response.output_text)
```

For streaming, declare `CallType.STREAMING`, pass the provider's native
`stream=True` argument, and iterate native typed Responses events:

```python
stream = responses_router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_RESPONSES,
            operation="responses.create",
            call_type=CallType.STREAMING,
            arguments={"input": "Write a short product description.", "stream": True},
        )
    ]
)

for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="")
```

Chat uses `messages` and yields Chat Completion chunks; Responses uses `input`
and yields typed events ending in `response.completed` or
`response.incomplete`. An incomplete response is a served result: it warns once
but does not fall back or bench the provider. Stored responses, continuation
IDs, conversations, and background lifecycles are provider-owned state; callers
must preserve strict endpoint/account affinity when using them.
`StickyRoutingPolicy` can provide a best-effort fixed preference, but health
filtering and fallback may still choose another provider. Stateless routed
calls are the safe interchangeable-provider pattern.

## LangChain

LangChain can call the same router explicitly through a `RunnableLambda`. The
router is responsible only for the model call; LangChain still owns prompt and
workflow composition.

```python
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda

prompt = PromptTemplate.from_template(
    "Write a short product description for {product}."
)

workflow = prompt | RunnableLambda(
    lambda prompt_value: call_router(prompt_value.to_string())
)
result = workflow.invoke({"product": "wireless headphones"})

print(result)
```

This is ordinary LangChain composition around an explicit `ProviderRouter`
call. It does not rely on a Nygen Router LangChain adapter.

## Pydantic

Ordinary Pydantic models can validate structured text returned through the same
router boundary.

```python
import json

from pydantic import BaseModel, Field


class ProductDescription(BaseModel):
    description: str = Field(min_length=1, max_length=300)


schema = json.dumps(ProductDescription.model_json_schema())
text = call_router(
    "Write a short product description for wireless headphones. "
    f"Return only valid JSON matching this schema: {schema}"
)
result = ProductDescription.model_validate_json(text)

print(result.description)
```

Pydantic validates the result after the provider call. This example uses no
Pydantic AI integration or router-specific Pydantic adapter. Close the shared
DuckDB store with `metrics.close()` when the application shuts down.

## Runtime provider selection

For each call, Nygen Router:

```text
1. Excludes disabled, unavailable, unsupported, or temporarily benched providers.
2. Orders eligible providers using the configured policy.
3. Sends the matching native CallVariant with the selected provider's model.
4. Optionally retries a reached provider before continuing through the computed order.
5. Falls back on retryable provider failures while eligible alternatives remain.
6. Records scoped provider-ID/model/protocol/call-type observations and stream timing.
7. Uses that recent history to improve later choices when ScoreBasedPolicy is enabled.
```

### Optional fixed provider preference

Select `StickyRoutingPolicy` explicitly when one or more canonical provider IDs
should always lead while eligible:

```python
router = ProviderRouter(
    providers=providers,
    metrics_scope="my-app",
    policy=StickyRoutingPolicy(
        sticky_provider_ids=["provider-a", "provider-b"],
        fallback_policy=ScoreBasedPolicy(),
    ),
)
```

The sticky IDs form a fixed ordered prefix; the wrapped policy orders only the
remaining eligible providers. Without `fallback_policy`, each sticky policy
gets its own `RoundRobinPolicy`. IDs must be a non-empty `list[str]` of configured
`provider_id` values; whitespace, blanks, duplicates, non-strings, and unknown
IDs are validated at construction.

This feature stores no learned affinity, key, TTL, or persistent state. A
successful fallback does not change the next call's preference. Health and hard
eligibility always take precedence, including for streaming restarts. Separate
router/policy instances are appropriate when users or workflows need different
preference lists. Fixed preference may help provider-local cache reuse or usage
concentration, but it cannot guarantee caching, quota behavior, or strict
provider-owned state continuity.

### Optional same-provider retry

Same-provider retry is disabled by default. Enable it explicitly and independently
from provider ordering with the compact recommended configuration:

```python
router = ProviderRouter(
    providers=providers,
    metrics_scope="my-app",
    retry_policy=SameProviderRetryPolicy(),
)
```

The default gives only the first provider in the already-computed order up to
three total physical attempts: its initial attempt plus at most two retries.
`RetryProviderScope.ALL` gives each distinct reached provider one cycle;
`RetryProviderScope.SELECTED` limits cycles to configured canonical provider
IDs. The built-in retries only timeout, connection, and server-error failures.
Authentication, rate limits, bad requests, invalid operations, a newly started
health bench, and every failure after a normalized stream opens are hard
exclusions. Provider SDK retries remain disabled.

Every physical attempt independently affects health, metrics, scoring history,
and exhaustion diagnostics. `HealthConfig.failure_threshold` is a circuit
breaker across physical attempts, not the retry budget; reaching it stops the
current provider's remaining retries. A streaming call may retry only when its
adapter fails before returning a `NormalizedStream`. Opened streams retain the
existing restart-or-raise behavior on the remaining provider tail.

**Replay risk:** the router cannot determine whether a native request is safe to
repeat. A timeout or disconnect does not prove the provider failed to receive or
process it. Retrying can duplicate provider work, tool side effects,
stored/background operations, charges, or other non-idempotent behavior.
`CallVariant.arguments` stays opaque: provider-native idempotency mechanisms pass
through unchanged but are neither created nor verified. Selecting
`retry_policy` is router-wide acceptance of this risk; use separate router
instances when different calls need different replay policies. There is no
per-call override, delay, backoff, `Retry-After`, async execution, or persistent
retry state. Values above eight total attempts clamp to eight with one
caller-facing `UserWarning`.

Capability inference, cost-aware routing, built-in adapters beyond the two
OpenAI protocols, and framework-specific adapters are not part of the current
shipped implementation. The examples above show the integration surface
available today: workflows keep their own structure and delegate only the
provider call to Nygen Router.

## Workflow examples

Manual LangChain and Pydantic workflow examples live under `WorkflowTests`.
They make real network calls and maintain local routing history, so review
[`WorkflowTests/README.md`](WorkflowTests/README.md) before running them.
