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
