from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda, RunnablePassthrough

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKFLOW_ROOT))

from shared import (  # noqa: E402
    DuckDBMetricsStore,
    ProviderConfig,
    ProviderRouter,
    invoke_regular,
    load_project_environment,
    open_metrics_store,
    parse_options,
    print_score_snapshot,
    provider_configs,
    require_api_keys,
    run_calibration,
    score_based_router,
)


def router_step(
    router: ProviderRouter,
    store: DuckDBMetricsStore,
    providers: list[ProviderConfig],
    *,
    label: str,
    max_tokens: int,
) -> RunnableLambda:
    """Make an explicit ProviderRouter call usable as one LangChain LCEL step."""

    def call_router(prompt_value: Any) -> str:
        result = invoke_regular(
            router,
            store,
            prompt=prompt_value.to_string(),
            label=label,
            max_tokens=max_tokens,
        )
        print_score_snapshot(providers, store, heading=f"Scores after {label}")
        return result.text

    return RunnableLambda(call_router)


def build_workflow(
    router: ProviderRouter,
    store: DuckDBMetricsStore,
    providers: list[ProviderConfig],
) -> Any:
    plan_prompt = PromptTemplate.from_template(
        "Topic: {topic}\n"
        "Create a tiny writing plan with exactly three short bullet points. "
        "Do not write the answer yet."
    )
    draft_prompt = PromptTemplate.from_template(
        "Topic: {topic}\nPlan:\n{plan}\n"
        "Write a simple draft of no more than three short sentences."
    )
    critique_prompt = PromptTemplate.from_template(
        "Topic: {topic}\nDraft:\n{draft}\n"
        "Give one strength and one concrete improvement. Keep both very short."
    )
    revision_prompt = PromptTemplate.from_template(
        "Topic: {topic}\nPlan:\n{plan}\nDraft:\n{draft}\nCritique:\n{critique}\n"
        "Write the improved final answer in no more than three short sentences. "
        "Return only the answer."
    )

    # Each RunnableLambda visibly delegates its model work to ProviderRouter.
    return (
        RunnablePassthrough.assign(
            plan=plan_prompt
            | router_step(
                router,
                store,
                providers,
                label="plan",
                max_tokens=512,
            )
        )
        .assign(
            draft=draft_prompt
            | router_step(
                router,
                store,
                providers,
                label="draft",
                max_tokens=512,
            )
        )
        .assign(
            critique=critique_prompt
            | router_step(
                router,
                store,
                providers,
                label="critique",
                max_tokens=512,
            )
        )
        .assign(
            final=revision_prompt
            | router_step(
                router,
                store,
                providers,
                label="revision",
                max_tokens=512,
            )
        )
    )


def main() -> None:
    options = parse_options("Run the low-cost LangChain ProviderRouter workflow.")
    load_project_environment()
    providers = provider_configs()
    require_api_keys(providers)
    store = open_metrics_store(reset_history=options.reset_history)

    try:
        run_calibration(providers, store)
        router = score_based_router(providers, store)
        workflow = build_workflow(router, store, providers)

        print(f"\nRunning LangChain workflow for: {options.topic}")
        result = workflow.invoke({"topic": options.topic})
        print("\nFinal answer")
        print(result["final"])
    finally:
        store.close()


if __name__ == "__main__":
    main()
