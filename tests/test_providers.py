from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

import pytest

from providerrouter.exceptions import ProviderError
from providerrouter.providers.anthropic import _split_system_message
from providerrouter.providers.openai_compat import OpenAICompatibleProvider


def test_provider_error_chains_original_exception(monkeypatch):
    original = RuntimeError("upstream failed")

    class FakeCompletions:
        def create(self, **kwargs):
            raise original

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    fake_openai_module = SimpleNamespace(OpenAI=FakeOpenAI)
    monkeypatch.setitem(sys.modules, "openai", fake_openai_module)

    provider = OpenAICompatibleProvider("openai", api_key="test", env_var="OPENAI_API_KEY")

    with pytest.raises(ProviderError) as exc_info:
        provider.call("gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert exc_info.value.original is original
    assert exc_info.value.__cause__ is original
    assert exc_info.value.provider == "openai"
    assert exc_info.value.model == "gpt-4o-mini"


def test_openai_compatible_adapter_descriptive_import_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "openai", raising=False)

    provider = OpenAICompatibleProvider("openai", api_key="test", env_var="OPENAI_API_KEY")

    with pytest.raises(ImportError) as exc_info:
        provider.call("gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert "providerrouter[openai]" in str(exc_info.value)


def test_anthropic_adapter_extracts_system_message():
    system, messages = _split_system_message(
        [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
    )

    assert system == "You are concise."
    assert messages == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]
