# nygen-router

`nygen-router` is a lightweight foundation for routing a request to one of several
providers that can serve the same model. It validates provider configuration,
normalizes input, filters out providers that cannot satisfy the request (hard
filters), and calls the first eligible OpenAI-compatible provider with `httpx`.

Only OpenAI-compatible `chat/completions` is implemented so far. Round robin,
fallback, SQLite memory, scoring, the Responses API, and framework adapters are
future PRs.

## Local Development

Requires **Python 3.12+**. Do not use a conda env on Python 3.10 for this package.

```sh
cd ProviderRouterPR1
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run tests with pytest (not by executing test files directly):

```sh
pytest
pytest tests/test_config.py -v
```

If you see `ModuleNotFoundError: No module named 'nygen_router'`, you are likely
using the wrong Python (for example conda's `provider-router` instead of
`.venv/bin/python`). Select the `.venv` interpreter in your IDE, or run:

```sh
.venv/bin/python -m pytest
```

## Minimal Usage

```python
from nygen_router import ApiProtocol, ProviderConfig, ProviderRouter

router = ProviderRouter(
    providers=[
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="my-model",
            base_url="https://api.provider-a.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        )
    ]
)

response = router.invoke("Hello")
print(response.text)
```

## Environment Variables

```sh
export PROVIDER_A_API_KEY="your-key"
```

API keys can also be passed explicitly with `api_key`, but keys are never printed
or included in router responses.

## Hard filtering and response transparency

Before routing, the router filters the full provider list down to providers that
can satisfy the request. Filters are hard, not scores: a provider that fails an
essential check — disabled, no API key available, unsupported protocol, or a
missing required capability (tool-calling, streaming, or JSON mode) — is
excluded, not ranked lower.

Every response reports what happened, so routing is never a black box:

- `response.attempts` — one `ProviderAttempt` per provider actually invoked. In
  this PR that is always exactly one (the provider that served the call);
  fallback across providers arrives in a later PR.
- `response.excluded` — one `EligibilityResult` per provider filtered out before
  any call, each carrying a specific `FilterReason` and human-readable `detail`.
  Populated on every call, success or not.

If filtering removes every configured provider, `invoke()` raises
`NoEligibleProvidersError`, whose message enumerates each excluded provider with
its own specific reason rather than a single blended summary.

```python
response = router.invoke("Hello")
print(response.text)
print([a.provider_name for a in response.attempts])          # provider(s) called
print([(e.provider_name, e.reason) for e in response.excluded])  # who was filtered, and why
```

## Errors

The router is deliberately transparent about failures — no "peel the onion"
debugging. The contract:

- **One base type.** Every error the router raises derives from
  `NygenRouterError`, so `except NygenRouterError` catches all of them. Specific
  types remain available for granular handling.
- **The exact provider error is preserved.** For a non-2xx response,
  `ProviderHTTPError` carries the provider's verbatim `message`, plus
  `status_code`, `error_type`, `error_code`, the full `body`, and the raw
  `response`. The standard `httpx.HTTPStatusError` stays reachable via
  `__cause__`.
- **The error type names the stage.** `ConfigError` / `MissingApiKeyError`
  (configuration), `NoProvidersConfiguredError` (no providers configured),
  `NoEligibleProvidersError` (all providers filtered out), `ProviderTimeoutError`
  / `ProviderConnectionError` / `ProviderError` (transport), `ProviderHTTPError`
  (HTTP status), `ProviderResponseError` (unparseable 2xx). Messages always name
  the provider and model.
- **Originals are chained, never re-wrapped.** Transport failures keep the exact
  `httpx` exception type in the message and attach it as both `__cause__` and
  `.original`.

```python
from nygen_router import ProviderHTTPError, ProviderRouter

try:
    router.invoke("Hello")
except ProviderHTTPError as error:
    print(error)             # Provider 'provider_a' returned HTTP 429 Too Many Requests ...
    print(error.status_code) # 429
    print(error.error_type)  # e.g. "rate_limit_exceeded"
    print(error.body)        # full provider error payload
    raise error.__cause__    # the underlying httpx.HTTPStatusError, if you want it
```

## Quality Checks

```sh
ruff format .
ruff check .
mypy src
pytest
coverage run -m pytest
coverage report
```
