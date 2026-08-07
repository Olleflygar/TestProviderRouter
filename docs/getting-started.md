# Getting started

`llm-provider-router` routes one native LLM call across providers that can serve
the same model. You keep the provider SDK's request shape and response type; the
router picks an eligible provider, injects that provider's configured `model`,
and returns the SDK response unchanged.

## Install

Requires **Python 3.12+**.

```sh
pip install "llm-provider-router[openai,duckdb]"
```

- Core import (`from llm_provider_router import ProviderRouter`) only needs
  `pydantic` — no provider SDK at import time.
- `[openai]` is required when you actually call OpenAI Chat or Responses
  adapters (including OpenAI-compatible `base_url`s).
- `[duckdb]` enables the default metrics store. Without it, calls still work;
  metrics writes degrade gracefully.

```sh
export PROVIDER_A_API_KEY="your-key"
```

Keys can also be passed as `api_key=` on `ProviderConfig`. They are never
printed in errors or logs.

## Minimal call

```python
from llm_provider_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ProviderConfig,
    ProviderRouter,
)

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

`response` is the real `openai` SDK object — not a wrapper. Do **not** put
`"model"` in `arguments`; the router injects it from `ProviderConfig`.

## Chat vs Responses

Both protocols use the official `openai` SDK. Chat uses
`chat.completions.create` and `messages`. Responses uses `responses.create` and
`input` (only that operation is routed):

```python
CallVariant(
    protocol=ApiProtocol.OPENAI_RESPONSES,
    operation="responses.create",
    call_type=CallType.REGULAR,
    arguments={
        "input": "Explain why the sky appears blue.",
        "instructions": "Answer in two sentences.",
    },
)
```

You can pass one `CallVariant` per protocol in the same `invoke()` so Chat and
Responses providers can fall back to each other. Every variant in one call must
share the same `call_type`.

Retrieval, deletion, cancellation, and conversation history stay on the native
SDK client — the router only runs model execution.

## How the router thinks

A few rules that show up everywhere else in these docs:

**Native pass-through.** `CallVariant.arguments` is opaque. The router does not
translate Chat ↔ Responses, validate tools/JSON mode, or reshape the response.
What you put in `arguments` is what the provider SDK gets (plus injected
`model`).

**Eligibility before routing.** Before any policy runs, providers that cannot
serve this call are excluded: disabled, auth-benched, in cooldown, missing API
key, unsupported protocol, or no matching `CallVariant` for their protocol.
That is eligibility filtering — config and health, not scores.

**No capability filtering.** The router does **not** inspect `arguments` to
guess whether a provider supports tools, streaming, or JSON mode. An
incompatible call fails at the provider like any other error.

**Fail-fast vs fallback.** Timeout, connection, 5xx, rate limit, auth, and
unknown errors can fall back to the next eligible provider. Bad requests
(400/422), bad `operation`/`arguments`, and a missing SDK stop the whole call
immediately — including across protocol variants — so a broken preferred path
stays visible.

**Identity.** `provider_id` is the stable key for metrics, health, and sticky
preference. `name` is display metadata only.

## Next

- [Policies](./policies.md) — round-robin, sticky, score-based, retry
- [Streaming](./streaming.md)
- [Metrics & storage](./metrics-storage.md)
- Full detail: [package README](../README.md)
