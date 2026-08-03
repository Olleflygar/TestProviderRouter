from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ProviderRouter" / "src"))

from nygen_router import (  # noqa: E402
    ApiProtocol,
    CallType,
    CallVariant,
    ProviderConfig,
    ProviderRouter,
)


class Answer(BaseModel):
    answer: str = Field(min_length=1, max_length=600)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a minimal one-step router workflow."
    )
    parser.add_argument(
        "--topic",
        default="Why short breaks can help people stay focused",
    )
    topic = parser.parse_args().topic
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    router = ProviderRouter(
        providers=[
            ProviderConfig(
                provider_id="fireworks:gpt-oss-20b",
                name="Fireworks",
                protocol=ApiProtocol.OPENAI_CHAT,
                model="accounts/fireworks/models/gpt-oss-20b",
                base_url="https://api.fireworks.ai/inference/v1",
                api_key_env="Fireworks_API_KEY",
            ),
            ProviderConfig(
                provider_id="together:gpt-oss-20b",
                name="TogetherAI",
                protocol=ApiProtocol.OPENAI_CHAT,
                model="OpenAI/gpt-oss-20B",
                base_url="https://api.together.ai/v1",
                api_key_env="Together_API_KEY",
            ),
        ],
        metrics_scope="workflow-tests:local",
    )

    prompt = (
        f"Topic: {topic}\n"
        "Answer in no more than three short sentences. "
        f"Return only valid JSON matching this schema: {json.dumps(Answer.model_json_schema())}"
    )
    response = router.invoke(
        [
            CallVariant(
                protocol=ApiProtocol.OPENAI_CHAT,
                operation="chat.completions.create",
                call_type=CallType.REGULAR,
                arguments={
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 512,
                    "reasoning_effort": "low",
                    "stream": False,
                },
            )
        ]
    )

    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The selected provider returned no text.")
    print(Answer.model_validate_json(content).answer)


if __name__ == "__main__":
    main()
