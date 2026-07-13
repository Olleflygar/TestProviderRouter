# ProviderRouterPR1 Agent Guide

Implementation rules for this package:

- Core imports must stay lightweight: `from nygen_router import ProviderRouter`
  must never require any provider SDK to be installed.
- No provider SDK imports at module level, anywhere -- not even inside an
  adapter module. Always lazy-import inside the method body that actually
  needs it (see `adapters/openai_compatible.py`'s `import openai` inside
  `invoke()`). `httpx` is not a core dependency either; it only appears as an
  injectable test seam (`http_client`) and as a transitive dependency of the
  `openai` extra.
- Do not add LangChain, Pydantic AI, DuckDB, Supabase, or OpenTelemetry yet.
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
- Hard filters (eligibility) run before routing and are static only: enabled,
  resolvable API key, protocol has a registered adapter, and protocol has a
  matching `CallVariant` in this specific call. There is currently no
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
  for the rest of the run (`FilterReason.AUTH_DISABLED_THIS_RUN`); a bad
  request (400/422), an unresolvable `operation`, or `arguments` that don't
  match the resolved operation's signature all stop the run immediately
  instead of trying more providers -- these are call-shape problems every
  provider sharing that protocol would hit identically, so continuing would
  only bury the real cause. Health state lives on `ProviderRouter` so the
  filter and any policy can see it. When every tried provider fails,
  `RouterExhaustedError` enumerates each real failure rather than blending
  them.

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
  `TypeError`, or SDK exception escape unwrapped.
- Never swallow or re-message a provider/transport error. Surface the
  provider's verbatim message and structured fields (status, error type/code,
  body) -- note that `openai.APIStatusError.message` is an SDK-synthesized
  summary, not the provider's own text; pull the real message out of
  `exc.body` (see `_verbatim_message` in `adapters/openai_compatible.py`).
- If you add context, chain it (`raise ... from original`) and also keep the
  original on `.original`. Never wrap an already-wrapped router error again.
- Prefer common terminology: HTTP status + reason phrase, and the exact
  `openai`/`httpx` exception type name for transport and dispatch failures.
