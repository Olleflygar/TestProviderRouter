from __future__ import annotations

from typing import Any

import pytest

from nygen_router import (
    ApiProtocol,
    CallVariant,
    FilterReason,
    NoEligibleProvidersError,
    ProviderConfig,
    ProviderConnectionError,
    ProviderHTTPError,
    ProviderRouter,
    ProviderTimeoutError,
    RouterExhaustedError,
    RoutingContext,
    UnsupportedOperationError,
)


def _config(name: str, *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol=ApiProtocol.OPENAI_CHAT,
        model="model-a",
        base_url=f"https://{name}.example.com/v1",
        api_key="secret",
        enabled=enabled,
    )


def _calls() -> list[CallVariant]:
    return [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            arguments={"messages": [{"role": "user", "content": "hi"}]},
        )
    ]


def _timeout(name: str) -> ProviderTimeoutError:
    return ProviderTimeoutError(f"Provider {name!r} timed out", provider_name=name, model="model-a")


def _http(name: str, status: int) -> ProviderHTTPError:
    return ProviderHTTPError(
        provider_name=name, model="model-a", status_code=status, message=f"status {status}"
    )


class _StaticPolicy:
    """Try eligible providers in config order (no rotation) for deterministic fallback tests."""

    def order(
        self, eligible: list[ProviderConfig], context: RoutingContext
    ) -> list[ProviderConfig]:
        return list(eligible)


class _ScriptedAdapter:
    """Adapter whose per-provider behavior is scripted: raise an exception or succeed.

    On success, returns the provider's own name as a sentinel -- there is no
    response wrapper anymore, so "who actually served the call" is the only
    thing worth returning for these tests.
    """

    def __init__(
        self,
        config: ProviderConfig,
        behaviors: dict[str, Exception],
        invoked: list[str],
    ):
        self.config = config
        self._behaviors = behaviors
        self._invoked = invoked

    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        self._invoked.append(self.config.name)
        behavior = self._behaviors.get(self.config.name)
        if behavior is not None:
            raise behavior
        return self.config.name


def _router(
    providers: list[ProviderConfig], behaviors: dict[str, Exception]
) -> tuple[ProviderRouter, list[str]]:
    invoked: list[str] = []

    def factory(config: ProviderConfig) -> _ScriptedAdapter:
        return _ScriptedAdapter(config, behaviors, invoked)

    router = ProviderRouter(providers=providers, adapter_factory=factory, policy=_StaticPolicy())
    return router, invoked


def test_fallback_tries_second_provider_on_timeout() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _timeout("provider_a")})

    response = router.invoke(_calls())

    assert response == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_rate_limit() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 429)})

    response = router.invoke(_calls())

    assert response == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_not_found() -> None:
    """A 404 is provider-specific (typo'd base_url, model not hosted) -- fall back."""
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 404)})

    response = router.invoke(_calls())

    assert response == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_http_408_timeout() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 408)})

    response = router.invoke(_calls())

    assert response == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_server_error() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 503)})

    response = router.invoke(_calls())

    assert response == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_connection_error() -> None:
    """A connection failure is provider-specific -- fall back rather than stop.

    Categorized as CONNECTION since PR5; a 404 still covers the UNKNOWN
    fallback path above.
    """
    connect_error = ProviderConnectionError(
        "could not connect", provider_name="provider_a", model="model-a"
    )
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": connect_error})

    response = router.invoke(_calls())

    assert response == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_auth_error_disables_provider_for_current_run() -> None:
    providers = [_config("provider_a")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 401)})

    with pytest.raises(RouterExhaustedError):
        router.invoke(_calls())
    assert invoked == ["provider_a"]

    # provider_a is now benched for the run: excluded (not re-tried) next call.
    with pytest.raises(NoEligibleProvidersError) as exc_info:
        router.invoke(_calls())

    excluded = {result.provider_name: result.reason for result in exc_info.value.exclusions}
    assert excluded["provider_a"] is FilterReason.AUTH_DISABLED_THIS_RUN


def test_bad_request_stops_immediately_without_trying_more_providers() -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 400)})

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke(_calls())

    assert invoked == ["provider_a"]  # provider_b and provider_c never tried
    assert [a.provider_name for a in exc_info.value.attempts] == ["provider_a"]


def test_bad_request_after_earlier_failure_keeps_both_attempts() -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    router, invoked = _router(
        providers,
        {"provider_a": _timeout("provider_a"), "provider_b": _http("provider_b", 400)},
    )

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke(_calls())

    assert invoked == ["provider_a", "provider_b"]  # provider_c never tried
    assert [a.provider_name for a in exc_info.value.attempts] == ["provider_a", "provider_b"]


def test_all_providers_fail_raises_with_each_distinct_reason() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, _ = _router(
        providers,
        {"provider_a": _timeout("provider_a"), "provider_b": _http("provider_b", 429)},
    )

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke(_calls())

    message = str(exc_info.value)
    assert "provider_a" in message
    assert "provider_b" in message
    assert "timed out" in message
    assert "429" in message
    assert [a.provider_name for a in exc_info.value.attempts] == ["provider_a", "provider_b"]


def test_unsupported_operation_stops_immediately_without_trying_more_providers() -> None:
    """A bad operation/arguments is a call-shape problem, not a per-provider one -- stop."""
    providers = [_config("provider_a"), _config("provider_b")]
    bad_op = UnsupportedOperationError("bad operation", provider_name="provider_a", model="model-a")
    router, invoked = _router(providers, {"provider_a": bad_op})

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke(_calls())

    assert invoked == ["provider_a"]  # provider_b never tried
    assert [a.provider_name for a in exc_info.value.attempts] == ["provider_a"]


def test_exhausted_error_attempts_are_json_serializable() -> None:
    """A live exception in attempts must not break model_dump_json()."""
    providers = [_config("provider_a")]
    router, _ = _router(providers, {"provider_a": _timeout("provider_a")})

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke(_calls())

    dumped = exc_info.value.attempts[0].model_dump_json()
    assert "ProviderTimeoutError" in dumped
    assert exc_info.value.attempts[0].error is not None  # real object still on the model


def test_exhausted_error_records_real_unwrapped_errors() -> None:
    """RouterExhaustedError.attempts carries the exact, unwrapped exception objects."""
    providers = [_config("provider_a"), _config("provider_b")]
    timeout = _timeout("provider_a")
    rate_limit = _http("provider_b", 429)
    router, _ = _router(providers, {"provider_a": timeout, "provider_b": rate_limit})

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke(_calls())

    assert exc_info.value.attempts[0].success is False
    assert exc_info.value.attempts[0].error is timeout  # exact, unwrapped exception object
    assert exc_info.value.attempts[1].success is False
    assert exc_info.value.attempts[1].error is rate_limit
