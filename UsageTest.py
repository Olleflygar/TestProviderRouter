from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ROUTER_SRC = PROJECT_ROOT / "ProviderRouterPR1" / "src"

# Makes this script runnable from the IDE play button without installing the package first.
sys.path.insert(0, str(ROUTER_SRC))

from nygen_router import ApiProtocol, ProviderConfig, ProviderRouter


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")

    router = ProviderRouter(
        providers=[
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
    )

    response = router.invoke("Write a short story about a cat")
    print(f"{response.provider_name} / {response.model}:")
    print(response.text)


if __name__ == "__main__":
    main()
