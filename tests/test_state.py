from __future__ import annotations

import pytest

from providerrouter.state import BaseState, InMemoryState


def test_in_memory_state_cycles_over_six_calls():
    state = InMemoryState()

    providers = ["a", "b", "c"]
    sequence = [state.get_next_provider(providers) for _ in range(6)]

    assert sequence == ["a", "b", "c", "a", "b", "c"]


def test_in_memory_state_single_provider():
    state = InMemoryState()

    providers = ["only"]
    sequence = [state.get_next_provider(providers) for _ in range(3)]

    assert sequence == ["only", "only", "only"]


def test_base_state_is_abstract():
    with pytest.raises(TypeError):
        BaseState()
