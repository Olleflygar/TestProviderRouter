from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ROUTER_SRC = PROJECT_ROOT / "ProviderRouter" / "src"

# Makes this script runnable from the IDE play button without installing the package first.
sys.path.insert(0, str(ROUTER_SRC))

from nygen_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ProviderConfig,
    ProviderRouter,
    RoundRobinPolicy,
)


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")

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
        metrics_scope="usage-test-round-robin:local",
        policy=RoundRobinPolicy(),
    )

    # With the round robin policy the leading provider rotates on each call, so
    # successive iterations should alternate between the configured providers.
    for i in range(4):
        response = router.invoke(
            [
                CallVariant(
                    protocol=ApiProtocol.OPENAI_CHAT,
                    operation="chat.completions.create",
                    call_type=CallType.REGULAR,
                    arguments={
                        "messages": [
                            {
                                "role": "user",
                                "content": ("Tell me something short."),
                            }
                        ],
                    },
                )
            ]
        )
        print(f"[{i}] {response.model}:")
        print(response.choices[0].message.content)
        print()


if __name__ == "__main__":
    main()
