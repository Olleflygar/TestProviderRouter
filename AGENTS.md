# ProviderRouter — agent guide

Read this when working anywhere under `ProviderRouter/`.

## Goal

Build a **drop-in LLM router** that selects the best OpenAI-compatible backend **per request**, with minimal configuration.

## Target user experience

```python
from provider_router import ProviderRouter

router = ProviderRouter(models=["gpt-4o-mini", "gpt-4.1", "qwen3:32b"])
response = router.invoke(messages)
```

No manual tiers, score cards, or routing logic in user code.

## Architecture (internal only)

| Module | Responsibility |
|--------|----------------|
| `ModelRegistry` | Model profiles: tier, cost, context, capabilities |
| `QueryAnalyzer` | Query features from `messages` at invoke time |
| `Scorer` | Per-model routing score → pick winner |
| `ProviderClient` | HTTP/OpenAI-compatible call to chosen backend |

Suggested layout when implementing:

```
ProviderRouter/
  provider_router/
    __init__.py          # ProviderRouter public class
    registry.py          # ModelRegistry
    analyzer.py            # QueryAnalyzer
    scorer.py              # Scorer
    client.py              # OpenAI-compatible client wrapper
    catalog.py             # Bundled model metadata
  tests/
  pseudo.py              # design sketch (may be replaced)
```

## Design constraints
# AGENTS.md — ProviderRouter

Coding rules for this repository. The goal is a **provider router**: given a model
the application wants to run, pick the provider (OpenAI, Azure, Fireworks, Together,
local, …) that gives the best result for *this specific call*, score providers live
as the app runs, and stay an invisible, thin layer.

A router's value is inverted from a framework's. A framework earns forgiveness for
abstraction because it does a lot. A router earns trust only by being invisible when
things work and brutally transparent when they don't. Never become the thing standing
between the developer and the truth of what their provider did.

---

## 1. Dependencies — the adoption gate

- **Keep the core dependency-free.** Stdlib only where possible; `httpx` is the
  heaviest thing the core may import. Installing the router must not perturb a
  user's environment.
- **Every provider SDK is an optional extra**, lazy-imported:
  `pip install providerrouter[openai,together]`. A missing SDK errors *only* when
  that provider is actually used, with a message naming the provider and the extra
  to install.
- **Never hard-pin versions.** Use loose ranges (`>=x,<next-major`). A minor bump of
  ours must never force a user to upgrade `openai`, `pydantic`, etc.
- Do not import a provider SDK at module top level. Import inside the provider
  adapter, at first use.
- No build artifacts (READMEs, data files) leaking into `site-packages/`.

## 2. Credentials — resolution chain, never a single source

- Resolution order for every key: **explicit argument → `os.environ` →
  lazy, descriptive failure.** Never "pick one."
- **Never call `load_dotenv()` or read a `.env` file inside the library.** `.env`
  is the application's concern. The library reads only `os.environ`.
- **Zero-config default:** when no `providers` argument is passed, auto-discover
  conventional env vars (`OPENAI_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`,
  `AZURE_API_KEY`, …) and silently enable the providers whose keys are present. An
  existing OpenAI user must be able to adopt us in one line.
- **Explicit dict overrides per-provider**, still falling back to env per-provider:
  ```python
  ProviderRouter(
      preferred_model="gpt-4o-mini",
      providers={
          "openai":   {"api_key": "sk-..."},
          "together": {"api_key": os.getenv("MY_VAR")},
          "fireworks": {},   # falls back to env discovery
      },
  )
  ```
- **Validate lazily, never at construction.** Do not hard-fail if a key is missing —
  local / no-auth endpoints (Ollama, self-hosted) are valid. Fail only when a call
  actually needs the missing key, naming the provider and the env var.

## 3. Public interface — mirror universal verbs, namespace router concepts

### Layer 1: invocation surface (mirror these EXACTLY — identical meaning everywhere)

| Method | Convention source | Notes |
|---|---|---|
| `invoke(messages, **kwargs)` | LangChain | single call → result |
| `ainvoke(messages, **kwargs)` | LangChain | async; `a`-prefix is universal |
| `stream(messages, **kwargs)` | LangChain | TRUE passthrough generator |
| `astream(messages, **kwargs)` | LangChain | async stream |
| `__call__(messages, **kwargs)` | smolagents | thin alias to `invoke` |

- The result object MUST expose **both** `.content` (LangChain / smolagents) and
  `.output` (Pydantic AI) so it duck-types into any host framework.
- The `a`-prefix is the async convention. Never invent `invoke_async` /
  `async_invoke`.

### Layer 2: router-specific surface (namespace under nouns — never bare methods)

```python
router.providers.scores()              # live observability
router.providers.health()
router.providers.add("together", api_key=...)
router.policy.set_fallback_order([...])
router.policy.scoring_weights = {...}  # application-adjustable metrics
router.audit.last_decision()           # per-decision trace
router.on_decision(callback)           # observability hook
```

### FORBIDDEN names (semantic collisions — worse than unfamiliar names)

- **`run`** — contested: Pydantic AI makes it async, smolagents/CrewAI make it sync.
  Never expose a bare `run`. Use the `invoke`/`ainvoke` split where sync-vs-async is
  unambiguous.
- **`with_fallbacks`**, **`with_retry`** — owned by LangChain's `Runnable` with
  specific "return a new wrapped Runnable" semantics. Our fallback/retry config lives
  under `router.policy`.
- **`bind_tools`**, **`with_structured_output`** — LangChain model semantics. Don't
  reuse unless behaviour is byte-for-byte identical.

If a host-framework adapter is ever needed (e.g. Pydantic AI), ship it as a separate
thin shim class, never by bending core method names.

## 4. Errors — total transparency

- **Never swallow a provider's exception** inside routing / scoring / fallback /
  retry logic. The provider's real error must always reach the caller.
- Never wrap in a generic `RouterError("something went wrong")`. If we add context,
  chain it (`raise RouterError(...) from provider_exc`) so the original is intact.
- Error messages name the **provider**, the **model**, and the **specific problem**
  (missing key, unsupported capability, rate limit, etc.).
- On fallback, the *original* upstream error must be preserved and surfaced, not
  replaced by an error from the fallback target.

## 5. Observability — first-class, not bolted on

This is the router's domain; it is the #1 source of issues against comparable tools.
Build it in from day one.

- **Queryable live state:** current provider scores and health must be inspectable
  (`router.providers.scores()` / `.health()`), not buried in logs.
- **Per-decision audit trail:** for each routed call, record chosen provider, the
  alternatives and their scores, observed latency, cost, and success/failure.
- **Decision hook:** `router.on_decision(callback)` so users pipe routing decisions
  into their own monitoring.
- **No silent traffic shifting.** Benching/cooling-down a provider or falling back
  must be observable via state + hook.
- **No silent retries.** Retries fire a hook/log entry; expose retry counts.

## 6. Scoring & routing logic

- **No pre-benchmarking.** Scores are built and updated live as the app runs.
- **Recency-weighted:** prioritise the last few days over weeks-old data; provider
  performance and settings drift.
- **Per-call, not global average:** the same model behaves differently per provider
  and per prompt size / time of day. Score against features of *this* request.
- **Application-adjustable metric weights** via `router.policy.scoring_weights`.
- **Track** latency, cost, success rate, rate-limit events, and per-(provider,model)
  capabilities over time.

## 7. Capability awareness — the "unified interface" is leaky

- Maintain a per-`(provider, model)` capability map: tool calling, streaming,
  JSON/structured output, reasoning, etc.
- Before routing, either route *around* an unsupported capability or **fail loudly**
  naming provider + missing capability. Never silently send a request a provider
  will mangle (the classic source of baffling cross-provider errors).
- Normalise provider-specific quirks (param name mismatches, reasoning/thinking
  fields, message-format differences) inside the adapter, not in the hot path.

## 8. Streaming & async — table stakes

- **True streaming only.** Yield chunks as they arrive. Never buffer the full
  response then release it at once ("fake streaming" is a recurring, hated bug).
- The scoring/audit hook fires on stream **completion** (a finalizer), since total
  latency/success aren't known until the stream ends.
- Native async on `ainvoke`/`astream` where the provider SDK supports it; fall back
  to a threadpool only as a default, and document it.
- Respect `retry_after`; use exponential backoff; make retry caps deterministic so a
  fallback can never loop forever.

## 9. Testing

- Ship **failure injection**: a `mock_response=Exception(...)` style hook and a
  force-fallback flag, so users can exercise routing without spending money.
- Every provider adapter has tests for: missing key, unsupported capability, rate
  limit, timeout, and a successful call.
- Test that the original error survives a fallback chain unaltered.

---

## TL;DR for every change

1. Did I add a dependency to the core? → Don't. Make it an optional extra,
   lazy-imported.
2. Did I read a `.env` file or hard-fail on a missing key? → Don't. `os.environ`
   only; validate lazily.
3. Did I add a bare method that collides with `run` / `with_fallbacks` /
   `with_retry` / `bind_tools`? → Move it under `router.providers` / `.policy` /
   `.audit`.
4. Did I swallow or rewrite a provider error? → Surface the original.
5. Did I shift traffic, retry, or fall back silently? → Add state + a hook.
6. Did I buffer a stream? → Yield chunks as they arrive.