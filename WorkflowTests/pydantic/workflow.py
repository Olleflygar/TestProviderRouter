from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

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

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ModelT = TypeVar("ModelT", bound=BaseModel)


class WritingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: list[ShortText] = Field(min_length=3, max_length=3)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: ShortText = Field(max_length=600)


class Critique(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strength: ShortText = Field(max_length=240)
    improvement: ShortText = Field(max_length=240)


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: ShortText = Field(max_length=600)


def validated_router_step(
    router: ProviderRouter,
    store: DuckDBMetricsStore,
    providers: list[ProviderConfig],
    *,
    label: str,
    instruction: str,
    result_type: type[ModelT],
    max_tokens: int,
) -> ModelT:
    """Call the router, validate JSON, and make one correction attempt if needed."""
    schema = json.dumps(result_type.model_json_schema(), separators=(",", ":"))
    prompt = f"{instruction}\nReturn only valid JSON matching this schema:\n{schema}"

    for attempt_number in range(1, 3):
        result = invoke_regular(
            router,
            store,
            prompt=prompt,
            label=f"{label} (attempt {attempt_number})",
            max_tokens=max_tokens,
        )
        print_score_snapshot(
            providers,
            store,
            heading=f"Scores after {label} attempt {attempt_number}",
        )
        try:
            return result_type.model_validate_json(_strip_json_fence(result.text))
        except ValidationError as exc:
            if attempt_number == 2:
                raise RuntimeError(
                    f"{label} failed Pydantic validation after one correction attempt."
                ) from exc
            prompt = (
                f"{instruction}\n"
                f"Your previous response was invalid:\n{result.text[:800]}\n"
                f"Validation error:\n{str(exc)[:600]}\n"
                f"Return only corrected JSON matching this schema:\n{schema}"
            )

    raise AssertionError("unreachable")


def _strip_json_fence(text: str) -> str:
    """Accept a JSON markdown fence while still rejecting explanatory prose."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def main() -> None:
    options = parse_options("Run the low-cost Pydantic ProviderRouter workflow.")
    load_project_environment()
    providers = provider_configs()
    require_api_keys(providers)
    store = open_metrics_store(reset_history=options.reset_history)

    try:
        run_calibration(providers, store)
        router = score_based_router(providers, store)

        print(f"\nRunning Pydantic workflow for: {options.topic}")
        plan = validated_router_step(
            router,
            store,
            providers,
            label="plan",
            instruction=(
                f"Topic: {options.topic}\n"
                "Create exactly three very short points for a simple writing plan."
            ),
            result_type=WritingPlan,
            max_tokens=512,
        )
        draft = validated_router_step(
            router,
            store,
            providers,
            label="draft",
            instruction=(
                f"Topic: {options.topic}\n"
                f"Plan: {plan.model_dump_json()}\n"
                "Write a draft of no more than three short sentences."
            ),
            result_type=Draft,
            max_tokens=512,
        )
        critique = validated_router_step(
            router,
            store,
            providers,
            label="critique",
            instruction=(
                f"Topic: {options.topic}\n"
                f"Draft: {draft.model_dump_json()}\n"
                "Give one short strength and one short concrete improvement."
            ),
            result_type=Critique,
            max_tokens=512,
        )
        final = validated_router_step(
            router,
            store,
            providers,
            label="revision",
            instruction=(
                f"Topic: {options.topic}\n"
                f"Plan: {plan.model_dump_json()}\n"
                f"Draft: {draft.model_dump_json()}\n"
                f"Critique: {critique.model_dump_json()}\n"
                "Write the improved final answer in no more than three short sentences."
            ),
            result_type=FinalAnswer,
            max_tokens=512,
        )

        print("\nFinal answer")
        print(final.answer)
    finally:
        store.close()


if __name__ == "__main__":
    main()
