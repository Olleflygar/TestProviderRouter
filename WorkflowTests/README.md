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
python WorkflowTests/score_based_workflow.py
python WorkflowTests/sticky_retry_workflow.py
python WorkflowTests/langchain/workflow.py
python WorkflowTests/pydantic/workflow.py
python WorkflowTests/pydantic/CompactWorkflow.py
```

Every listed script accepts a simple custom topic:

```bash
python WorkflowTests/score_based_workflow.py \
  --topic "Why drinking water is important"
```

The decision demonstrations and the full LangChain/Pydantic workflows also
accept `--reset-history` to delete the shared metrics database before the run:

```bash
python WorkflowTests/pydantic/workflow.py --reset-history
```

History is preserved by default. Because DuckDB permits only one process to
write a database file, do not run these scripts concurrently.
PR29 does not migrate legacy schemas: if this file predates PR29, its owner
must pass `--reset-history` (or move it aside manually) before using these
development-only workflows. Router runtime code never deletes it automatically.

## Score-based decision demonstration

`score_based_workflow.py` is the most explicit score-based example:

1. Make two round-robin calibration rounds: four tiny regular calls, giving
   each provider two opportunities to lead.
2. Build a new router over the same metrics store with
   `ScoreBasedPolicy()`. The regular `CallType` comes from each invocation's
   `RoutingContext` automatically.
3. Run three short model steps: plan, draft, and revision.
4. Concisely print the ranked provider order, each persisted attempt's outcome
   and latency, and the provider that ultimately served the step.

The final comparison table pools all matching regular attempts in the shared
DuckDB across executions. It compares attempts, successes, success rate,
average successful-call latency, error tallies, and score components per
provider. The current metrics schema has no run identifier, so these are pooled
history averages rather than an average of separately identified runs.

## Sticky retry decision demonstration

`sticky_retry_workflow.py` makes two live workflow calls through a sticky
policy. The first call succeeds through the preferred provider. Before the
second call, its public adapter factory is instructed to raise two demonstration
HTTP 503 errors for that provider. `SameProviderRetryPolicy(max_attempts=2)`
therefore makes the initial attempt and one immediate same-provider retry before
the router continues to the real fallback provider.

The failures are deterministic and local to the demonstration; no failing HTTP
request is sent to the provider. Concise policy order, injected failures, retry
decisions, physical-attempt metrics, and the provider that ultimately served
each step are printed. It finishes with the same pooled shared-history
comparison table as the score-based demonstration.

## Shared behavior

All calls declare `CallType.REGULAR`, set the native `stream=False` argument,
and use low reasoning effort. Providers use stable IDs and the shared
`workflow-tests:local` metrics scope. Calibration calls
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
omits calibration, an explicit same-provider retry policy, and diagnostic
output.
