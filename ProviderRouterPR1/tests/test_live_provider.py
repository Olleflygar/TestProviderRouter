"""Live integration test: proves the provider configuration works against a real API.

Unlike the adapter tests (which use httpx.MockTransport), this test sends one
real request to the provider configured below. It auto-skips when the API key
environment variable is not set, so plain ``pytest`` stays free and offline on
machines without credentials.

To run it: ``export OPENAI_API_KEY=sk-...`` then ``pytest tests/test_live_provider.py``.
"""

from __future__ import annotations

import os
import string

import pytest

from nygen_router import ApiProtocol, ChatMessage, ProviderConfig, RouterRequest
from nygen_router.router import ProviderRouter

from dotenv import load_dotenv
import os
load_dotenv()



# --- EDIT ME: point this at the provider you want to verify -----------------
LIVE_PROVIDER_NAME = "DeepInfra"
LIVE_BASE_URL = "https://api.deepinfra.com/v1"
LIVE_MODEL = "deepseek-r1"
LIVE_API_KEY_ENV = os.environ["DeepInfra_API_KEY"]  # env var holding the key; never put the key itself here
# -----------------------------------------------------------------------------

requires_live_key = pytest.mark.skipif(
    not os.environ.get(LIVE_API_KEY_ENV),
    reason=f"set {LIVE_API_KEY_ENV} to run live provider tests",
)


def _live_config() -> ProviderConfig:
    return ProviderConfig(
        name=LIVE_PROVIDER_NAME,
        protocol=ApiProtocol.OPENAI_CHAT,
        model=LIVE_MODEL,
        base_url=LIVE_BASE_URL,
        api_key_env=LIVE_API_KEY_ENV,
    )


def _normalize(text: str) -> str:
    """Lowercase and strip whitespace/punctuation so 'Hello.' and ' hello ' match."""
    return text.strip().strip(string.punctuation).strip().lower()


@requires_live_key
def test_live_provider_returns_hello() -> None:
    router = ProviderRouter(providers=[_live_config()])

    request = RouterRequest(
        messages=[
            ChatMessage(
                role="user",
                content="Return only the word Hello if the connection is working, nothing else.",
            )
        ],
        temperature=0.0,
        max_tokens=10,
    )

    response = router.invoke(request)

    assert response.provider_name == LIVE_PROVIDER_NAME
    assert response.model == LIVE_MODEL
    assert _normalize(response.text) == "hello", f"unexpected reply: {response.text!r}"
