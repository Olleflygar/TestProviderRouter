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
    CallVariant,
    DuckDBMetricsStore,
    ProviderConfig,
    ProviderRouter,
    ScoreBasedPolicy,
)

metrics = DuckDBMetricsStore("router_metrics.duckdb")

router = ProviderRouter(
    providers=[
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="provider-a/model-name",
            base_url="https://provider-a.example.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        ),
        ProviderConfig(
            name="provider_b",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="provider-b/model-name",
            base_url="https://provider-b.example.com/v1",
            api_key_env="PROVIDER_B_API_KEY",
        ),
    ],
    policy=ScoreBasedPolicy(use_streaming=False),
    metrics_store=metrics,
)

try:
    response = router.invoke(
        [
            CallVariant(
                protocol=ApiProtocol.OPENAI_CHAT,
                operation="chat.completions.create",
                arguments={
                    "messages": [
                        {
                            "role": "user",
                            "content": "Write a short product description.",
                        }
                    ],
                    "stream": False,
                },
            )
        ]
    )
    print(response.choices[0].message.content)
finally:
    metrics.close()
```

`model` is deliberately absent from `CallVariant.arguments`: the router inserts
the model configured for whichever provider it selects. API keys can also be
passed directly to `ProviderConfig`, but environment-variable names keep secrets
out of source code.

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


def call_router(prompt_value) -> str:
    response = router.invoke(
        [
            CallVariant(
                protocol=ApiProtocol.OPENAI_CHAT,
                operation="chat.completions.create",
                arguments={
                    "messages": [
                        {"role": "user", "content": prompt_value.to_string()}
                    ],
                    "stream": False,
                },
            )
        ]
    )
    return response.choices[0].message.content or ""


workflow = prompt | RunnableLambda(call_router)
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
response = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Write a short product description for wireless headphones. "
                            f"Return only valid JSON matching this schema: {schema}"
                        ),
                    }
                ],
                "stream": False,
            },
        )
    ]
)
content = response.choices[0].message.content or ""
result = ProductDescription.model_validate_json(content)

print(result.description)
```

Pydantic validates the result after the provider call. This example uses no
Pydantic AI integration or router-specific Pydantic adapter.

## Runtime provider selection

For each call, Nygen Router:

```text
1. Excludes disabled, unavailable, unsupported, or temporarily benched providers.
2. Orders eligible providers using the configured policy.
3. Sends the matching native CallVariant with the selected provider's model.
4. Falls back on retryable provider failures while eligible alternatives remain.
5. Records success, latency, errors, and stream timing in the configured metrics store.
6. Uses that recent history to improve later choices when ScoreBasedPolicy is enabled.
```

Capability inference, cost-aware routing, built-in adapters for additional
provider protocols, and framework-specific adapters are not part of the current
shipped implementation. The examples above show the integration surface
available today: workflows keep their own structure and delegate only the
provider call to Nygen Router.
