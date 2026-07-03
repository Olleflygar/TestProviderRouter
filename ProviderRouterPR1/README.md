# nygen-router

`nygen-router` is a lightweight foundation for routing a request to one of several
providers that can serve the same model. PR 1 intentionally keeps behavior simple:
it validates provider configuration, normalizes input, checks basic requested
capabilities, and calls the first enabled OpenAI-compatible provider with `httpx`.

PR 1 only supports OpenAI-compatible `chat/completions`. Round robin, SQLite,
scoring, the Responses API, and framework adapters are future PRs.

## Local Development

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
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
  (configuration), `CapabilityError` (requested capability unavailable),
  `NoProvidersConfiguredError` (selection), `UnsupportedProtocolError` (adapter
  selection), `ProviderTimeoutError` / `ProviderConnectionError` /
  `ProviderError` (transport), `ProviderHTTPError` (HTTP status),
  `ProviderResponseError` (unparseable 2xx). Messages always name the provider
  and model.
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
