# ProviderRouterPR1 Agent Guide

Implementation rules for this PR 1 package:

- Core imports must stay lightweight.
- No provider SDK imports in core.
- Provider-specific SDKs must be lazy-imported in adapters if they are added later.
- Do not add LangChain, Pydantic AI, DuckDB, Supabase, or OpenTelemetry in PR 1.
- Do not leak API keys in errors, logs, responses, or tests.
- Use typed models, not raw dictionaries in core APIs.
- Required tests must not require real API keys (a live provider test may exist,
  but it must skip when its key is unset).
- Only OpenAI-compatible chat/completions is implemented so far. PR 2 adds hard
  filters (eligibility) that run before routing; excluded providers are reported
  on `RouterResponse.excluded`, and every unsupported protocol or missing
  required capability is a filter exclusion, not a raised error.

## Error transparency (non-negotiable)

Avoid the "peel the onion" debugging that plagues comparable routers:

- Every router error derives from `NygenRouterError`; the type names the stage
  that failed and the message names the provider and model.
- Never swallow or re-message a provider/transport error. Surface the provider's
  verbatim message and structured fields (status, error type/code, body).
- If you add context, chain it (`raise ... from original`) and also keep the
  original on `.original`. Never wrap an already-wrapped router error again.
- Prefer common terminology: HTTP status + reason phrase, and the exact `httpx`
  exception type name for transport failures.
