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
4. Falls back on retryable provider failures while eligible alternatives remain.
5. Records scoped provider-ID/model/protocol/call-type observations and stream timing.
6. Uses that recent history to improve later choices when ScoreBasedPolicy is enabled.
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

Capability inference, cost-aware routing, built-in adapters beyond the two
OpenAI protocols, and framework-specific adapters are not part of the current
shipped implementation. The examples above show the integration surface
available today: workflows keep their own structure and delegate only the
provider call to Nygen Router.

## Workflow examples

Manual LangChain and Pydantic workflow examples live under `WorkflowTests`.
They make real network calls and maintain local routing history, so review
[`WorkflowTests/README.md`](WorkflowTests/README.md) before running them.
