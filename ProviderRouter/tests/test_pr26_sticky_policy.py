from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from metrics_store_helpers import aggregate_events_for_score_query

from nygen_router import (
    ApiProtocol,
    CallType,
    ConfigError,
    MetricsEvent,
    ProviderConfig,
    ProviderRouter,
    RoutingContext,
    ScoreAggregate,
    ScoreAggregateQuery,
    ScoreBasedPolicy,
    StickyRoutingPolicy,
)


def _provider(
    provider_id: str,
    *,
    name: str | None = None,
    enabled: bool = True,
) -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        name=provider_id if name is None else name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{provider_id}.example.com/v1",
        api_key="secret",
        enabled=enabled,
    )


def _context(*, metrics_store: Any = None) -> RoutingContext:
    return RoutingContext(
        metrics_store=metrics_store,
        metrics_scope="test",
        call_type=CallType.REGULAR,
    )


class _RecordingPolicy:
    def __init__(self, result: list[ProviderConfig] | None = None) -> None:
        self.result = result
        self.calls: list[tuple[list[ProviderConfig], RoutingContext]] = []

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        self.calls.append((eligible, context))
        return list(reversed(eligible)) if self.result is None else list(self.result)


class _IdentityPolicy:
    """Stateless wrapped policy safe to share across test threads."""

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(eligible)


@pytest.mark.parametrize("value", [(), ("a",), {"a"}, "a", None])
def test_sticky_provider_ids_must_be_a_list(value: object) -> None:
    with pytest.raises(ConfigError, match="must be a list"):
        StickyRoutingPolicy(sticky_provider_ids=cast(Any, value))


def test_sticky_provider_ids_must_not_be_empty() -> None:
    with pytest.raises(ConfigError, match="at least one"):
        StickyRoutingPolicy(sticky_provider_ids=[])


@pytest.mark.parametrize("value", [1, None])
def test_sticky_provider_ids_must_be_strings(value: object) -> None:
    with pytest.raises(ConfigError, match="Sticky provider IDs must be strings"):
        StickyRoutingPolicy(sticky_provider_ids=cast(Any, [value]))


@pytest.mark.parametrize("value", ["", "   "])
def test_sticky_provider_ids_must_not_be_blank(value: str) -> None:
    with pytest.raises(ConfigError, match="must not be empty"):
        StickyRoutingPolicy(sticky_provider_ids=[value])


def test_sticky_provider_ids_are_trimmed_copied_and_deduplicated_after_trimming() -> None:
    configured = ["  preferred  "]
    policy = StickyRoutingPolicy(sticky_provider_ids=configured)
    configured[0] = "changed"
    preferred = _provider("preferred")
    other = _provider("other")

    assert policy.order([other, preferred], _context()) == [preferred, other]

    with pytest.raises(ConfigError, match="Duplicate sticky provider ID.*preferred"):
        StickyRoutingPolicy(sticky_provider_ids=[" preferred", "preferred "])


def test_router_rejects_every_unknown_sticky_id_and_never_uses_display_name() -> None:
    configured = _provider("canonical-id", name="display-name")
    policy = StickyRoutingPolicy(sticky_provider_ids=["display-name", "also-unknown"])

    with pytest.raises(ConfigError) as exc_info:
        ProviderRouter(
            providers=[configured],
            metrics_scope="test",
            metrics_store=None,
            policy=policy,
        )

    message = str(exc_info.value)
    assert "display-name" in message
    assert "also-unknown" in message


def test_duplicate_display_names_are_valid_when_provider_ids_are_unique() -> None:
    ProviderRouter(
        providers=[_provider("a", name="same"), _provider("b", name="same")],
        metrics_scope="test",
        metrics_store=None,
        policy=StickyRoutingPolicy(sticky_provider_ids=["a"]),
    )


def test_configured_sticky_order_leads_and_wrapped_policy_gets_fresh_remainder() -> None:
    a, b, c, d = (_provider(item) for item in "abcd")
    fallback = _RecordingPolicy()
    policy = StickyRoutingPolicy(sticky_provider_ids=["b", "a"], fallback_policy=fallback)
    context = _context()
    eligible = [a, c, b, d]

    result = policy.order(eligible, context)

    assert result == [b, a, d, c]
    assert len(fallback.calls) == 1
    received, received_context = fallback.calls[0]
    assert received == [c, d]
    assert received is not eligible
    assert received_context is context


def test_wrapped_policy_is_called_once_when_every_eligible_provider_is_sticky() -> None:
    a, b = _provider("a"), _provider("b")
    fallback = _RecordingPolicy()
    policy = StickyRoutingPolicy(sticky_provider_ids=["b", "a"], fallback_policy=fallback)

    assert policy.order([a, b], _context()) == [b, a]
    assert len(fallback.calls) == 1
    assert fallback.calls[0][0] == []


def test_wrapped_policy_omissions_and_duplicates_are_preserved() -> None:
    sticky, a, b = _provider("sticky"), _provider("a"), _provider("b")
    fallback = _RecordingPolicy(result=[b, b])
    policy = StickyRoutingPolicy(sticky_provider_ids=["sticky"], fallback_policy=fallback)

    assert policy.order([a, sticky, b], _context()) == [sticky, b, b]


@pytest.mark.parametrize("introduced_id", ["sticky", "unknown", "filtered"])
def test_wrapped_policy_cannot_introduce_a_provider_outside_its_remainder(
    introduced_id: str,
) -> None:
    sticky = _provider("sticky")
    remainder = _provider("remainder")
    introduced = _provider(introduced_id)
    fallback = _RecordingPolicy(result=[introduced])
    policy = StickyRoutingPolicy(sticky_provider_ids=["sticky"], fallback_policy=fallback)

    with pytest.raises(ConfigError, match="not in its eligible non-sticky remainder"):
        policy.order([sticky, remainder], _context())


def test_wrapper_uses_canonical_eligible_object_for_an_allowed_returned_id() -> None:
    sticky = _provider("sticky")
    canonical = _provider("remainder", name="canonical")
    foreign = _provider("remainder", name="foreign")
    fallback = _RecordingPolicy(result=[foreign])
    policy = StickyRoutingPolicy(sticky_provider_ids=["sticky"], fallback_policy=fallback)

    result = policy.order([canonical, sticky], _context())

    assert result[1] is canonical


def test_default_round_robin_policies_are_independent_and_rotate_only_the_tail() -> None:
    sticky, a, b = _provider("sticky"), _provider("a"), _provider("b")
    first = StickyRoutingPolicy(sticky_provider_ids=["sticky"])
    second = StickyRoutingPolicy(sticky_provider_ids=["sticky"])
    eligible = [sticky, a, b]
    context = _context()

    assert first.order(eligible, context) == [sticky, a, b]
    assert first.order(eligible, context) == [sticky, b, a]
    assert second.order(eligible, context) == [sticky, a, b]


class _MemoryStore:
    def __init__(self, events: list[MetricsEvent]) -> None:
        self.events = events

    def record_attempt(self, event: MetricsEvent) -> None:
        self.events.append(event)

    def query_recent(self, **kwargs: Any) -> list[MetricsEvent]:
        return list(self.events)

    def query_score_aggregates(self, query: ScoreAggregateQuery) -> list[ScoreAggregate]:
        return aggregate_events_for_score_query(self.events, query)


def test_score_based_policy_ranks_only_the_non_sticky_tail() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    sticky, slow, fast = (
        _provider("sticky"),
        _provider("slow"),
        _provider("fast"),
    )
    events = [
        MetricsEvent(
            metrics_scope="test",
            provider_id=provider_id,
            provider_name=provider_id,
            model="model-a",
            protocol=ApiProtocol.OPENAI_CHAT,
            call_type=CallType.REGULAR,
            success=success,
            latency_ms=latency,
            timestamp=now,
        )
        for provider_id, success, latency in [
            *(("fast", True, 10.0) for _ in range(20)),
            *(("slow", False, None) for _ in range(20)),
            *(("sticky", False, None) for _ in range(100)),
        ]
    ]
    store = _MemoryStore(events)
    policy = StickyRoutingPolicy(
        sticky_provider_ids=["sticky"],
        fallback_policy=ScoreBasedPolicy(now=lambda: now),
    )

    assert policy.order([slow, sticky, fast], _context(metrics_store=store)) == [
        sticky,
        fast,
        slow,
    ]


def test_concurrent_orders_compute_independent_fixed_prefixes() -> None:
    sticky = _provider("sticky")
    chat_tail = _provider("chat-tail")
    other_tail = _provider("other-tail")
    policy = StickyRoutingPolicy(sticky_provider_ids=["sticky"], fallback_policy=_IdentityPolicy())
    inputs = [[chat_tail, sticky], [other_tail], [sticky], [other_tail, sticky]] * 20

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda providers: policy.order(providers, _context()), inputs))

    assert results[0] == [sticky, chat_tail]
    assert results[1] == [other_tail]
    assert results[2] == [sticky]
    assert results[3] == [sticky, other_tail]
