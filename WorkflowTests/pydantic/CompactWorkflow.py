from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = WORKFLOW_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "ProviderRouterPR1" / "src"))

from nygen_router import (  # noqa: E402
    ApiProtocol,
    CallVariant,
    DuckDBMetricsStore,
    ProviderConfig,
    ProviderRouter,
    ScoreBasedPolicy,
)

DEFAULT_TOPIC = "Why short breaks can help people stay focused"
ModelT = TypeVar("ModelT", bound=BaseModel)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: list[str] = Field(min_length=3, max_length=3)


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=600)


def invoke(router: ProviderRouter, prompt: str, result_type: type[ModelT]) -> ModelT:
    schema = json.dumps(result_type.model_json_schema(), separators=(",", ":"))
    response = router.invoke(
        [
            CallVariant(
                protocol=ApiProtocol.OPENAI_CHAT,
                operation="chat.completions.create",
                arguments={
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"{prompt}\n"
                                f"Return only valid JSON matching this schema:\n{schema}"
                            ),
                        }
                    ],
                    "max_tokens": 1024,
                    "reasoning_effort": "low",
                    "stream": False,
                },
            )
        ]
    )
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The selected provider returned no text.")
    return result_type.model_validate_json(_strip_json_fence(content))


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return stripped


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a compact two-step router workflow.")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    topic = parser.parse_args().topic

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    providers = [
        ProviderConfig(
            name="Fireworks",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="accounts/fireworks/models/gpt-oss-20b",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="Fireworks_API_KEY",
        ),
        ProviderConfig(
            name="TogetherAI",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="OpenAI/gpt-oss-20B",
            base_url="https://api.together.ai/v1",
            api_key_env="Together_API_KEY",
        ),
    ]
    for provider in providers:
        provider.resolve_api_key()

    store = DuckDBMetricsStore(WORKFLOW_ROOT / "workflow_history.duckdb")
    if not store.available:
        raise RuntimeError("DuckDB is required. Install WorkflowTests/requirements.txt.")

    try:
        router = ProviderRouter(
            providers=providers,
            policy=ScoreBasedPolicy(use_streaming=False),
            metrics_store=store,
        )
        plan = invoke(
            router,
            (
                f"Topic: {topic}\n"
                "Create exactly three writing-plan points of no more than ten words each."
            ),
            Plan,
        )
        final = invoke(
            router,
            (
                f"Topic: {topic}\n"
                f"Plan: {plan.model_dump_json()}\n"
                "Write the final answer in no more than three short sentences."
            ),
            FinalAnswer,
        )
        print(final.answer)
    finally:
        store.close()


if __name__ == "__main__":
    main()
