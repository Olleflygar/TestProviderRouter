# ProviderRouterPR1 Agent Guide

Implementation rules for this PR 1 package:

- Core imports must stay lightweight.
- No provider SDK imports in core.
- Provider-specific SDKs must be lazy-imported in adapters if they are added later.
- Do not add LangChain, Pydantic AI, DuckDB, Supabase, or OpenTelemetry in PR 1.
- Do not leak API keys in errors, logs, responses, or tests.
- Use typed models, not raw dictionaries in core APIs.
- Tests must not require real API keys.
- PR 1 only supports OpenAI-compatible chat/completions.

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
