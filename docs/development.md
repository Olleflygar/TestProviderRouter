# Development

Requires **Python 3.12+**. Prefer a venv over an unrelated conda env.

```sh
cd ProviderRouter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Tests

Use pytest (don't execute test files as scripts):

```sh
pytest
pytest tests/test_config.py -v
```

If you see `ModuleNotFoundError: No module named 'llm_provider_router'`, you're
probably on the wrong interpreter:

```sh
.venv/bin/python -m pytest
```

Required tests must not need real API keys. Live provider tests may exist but
should skip when their key is unset.

## Quality checks

```sh
ruff format .
ruff check .
mypy src
pytest
coverage run -m pytest
coverage report
```

## Package layout notes

- Core imports stay SDK-free: no `openai` / `httpx` / `duckdb` / `psycopg` at
  module level — lazy-import inside the methods that need them.
- Source and tests define shipped behavior. Roadmap lives under
  [`../Projectplan/NewProjectPlan.md`](../Projectplan/NewProjectPlan.md).
- Agent/implementation rules: [`../ProviderRouter/AGENTS.md`](../ProviderRouter/AGENTS.md).
