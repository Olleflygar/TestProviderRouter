# ProviderRouter workflow usage tests

These are manual, networked integration scripts. They use the Fireworks and
TogetherAI provider/model configurations from `UsageTestRoundRobin.py` and
write provider-attempt metrics to the same local database:

```text
WorkflowTests/workflow_history.duckdb
```

The database contains router metrics only. Prompts and generated text are not
persisted.

## Setup

From the repository root, install the script dependencies:

```bash
python -m pip install -r WorkflowTests/requirements.txt
```

Configure `Fireworks_API_KEY` and `Together_API_KEY` in the repository-root
`.env` file or in the process environment. The scripts contain no API keys.

## Run

```bash
python WorkflowTests/langchain/workflow.py
python WorkflowTests/pydantic/workflow.py
python WorkflowTests/pydantic/CompactWorkflow.py
```

Both accept a simple custom topic:

```bash
python WorkflowTests/langchain/workflow.py \
  --topic "Why drinking water is important"
```

Pass `--reset-history` to delete the shared metrics database before the run:

```bash
python WorkflowTests/pydantic/workflow.py --reset-history
```

History is preserved by default. Because DuckDB permits only one process to
write a database file, do not run the two scripts concurrently.

## What each run does

1. Make two round-robin calibration rounds: four tiny regular calls, giving
   each provider two opportunities to lead.
2. Build a new router over the same metrics store with
   `ScoreBasedPolicy(use_streaming=False)`.
3. Run four short model steps: plan, draft, critique, and revision.
4. Print every provider attempt plus regular success, latency, and score
   components after each scored step.

All calls set `stream=False` and use low reasoning effort. Calibration calls
are capped at 128 total generated tokens and workflow calls at 512. These are
ceilings that include reasoning tokens; the model can stop earlier. They avoid
the empty responses that GPT-OSS can produce when reasoning consumes an
overly-small limit while keeping the simple workflow inexpensive.

The LangChain script uses prompt templates and LCEL runnable steps. Each model
step explicitly calls `ProviderRouter`; it does not disguise the router as a
LangChain chat model.

The Pydantic script uses ordinary Pydantic models rather than Pydantic AI. It
validates structured JSON between model steps. A validation failure gets one
correction call, after which the script stops with a clear error.

`pydantic/CompactWorkflow.py` is the minimal integration example. Its two-step
workflow depends only on a small `TextModel` protocol, while `RouterTextModel`
adapts `ProviderRouter` to that interface. Replacing the model object does not
change the workflow. It uses the shared DuckDB and score-based routing but
omits calibration, retries, and diagnostic output.
