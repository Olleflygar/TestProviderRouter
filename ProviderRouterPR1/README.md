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

## Quality Checks

```sh
ruff format .
ruff check .
mypy src
pytest
coverage run -m pytest
coverage report
```
