from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ROUTER_SRC = PROJECT_ROOT / "ProviderRouter" / "src"

# Makes this script runnable from the IDE play button without installing the package first.
sys.path.insert(0, str(ROUTER_SRC))

from llm_provider_router import (
    ApiProtocol,
    CallType,
    CallVariant,
    ProviderConfig,
    ProviderRouter,
    SameProviderRetryPolicy,
    StickyRoutingPolicy,
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
        metrics_scope="usage-test:local",
        policy=StickyRoutingPolicy(
            sticky_provider_ids=["fireworks:gpt-oss-20b"],
        ),
        # Explicit opt-in: three total attempts on the first reached provider.
        # Native calls may not be safe to replay; use only when that risk is accepted.
        retry_policy=SameProviderRetryPolicy(),
    )

    response = router.invoke(
        [
            CallVariant(
                protocol=ApiProtocol.OPENAI_CHAT,
                operation="chat.completions.create",
                call_type=CallType.REGULAR,
                arguments={
                    "messages": [{"role": "user", "content": "Tell a short joke"}],
                },
            )
        ]
    )
    print(f"{response.model}:")
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
