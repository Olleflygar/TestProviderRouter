from __future__ import annotations

import pytest

from nygen_router import (
    ApiProtocol,
    FilterReason,
    ProviderConfig,
    ProviderConnectionError,
    ProviderHTTPError,
    ProviderRouter,
    ProviderTimeoutError,
    RouterExhaustedError,
    RouterResponse,
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


def _timeout(name: str) -> ProviderTimeoutError:
    return ProviderTimeoutError(f"Provider {name!r} timed out", provider_name=name, model="model-a")


def _http(name: str, status: int) -> ProviderHTTPError:
    return ProviderHTTPError(
        provider_name=name, model="model-a", status_code=status, message=f"status {status}"
    )


class _StaticPolicy:
    """Try eligible providers in config order (no rotation) for deterministic fallback tests."""

    def order(self, eligible: list[ProviderConfig]) -> list[ProviderConfig]:
        return list(eligible)


class _ScriptedAdapter:
    """Adapter whose per-provider behavior is scripted: raise an exception or succeed."""

    def __init__(
        self,
        config: ProviderConfig,
        behaviors: dict[str, Exception],
        invoked: list[str],
    ):
        self.config = config
        self._behaviors = behaviors
        self._invoked = invoked

    def invoke(self, request: object) -> RouterResponse:
        self._invoked.append(self.config.name)
        behavior = self._behaviors.get(self.config.name)
        if behavior is not None:
            raise behavior
        return RouterResponse(provider_name=self.config.name, model=self.config.model, text="ok")


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

    response = router.invoke("hi")

    assert response.provider_name == "provider_b"
    assert invoked == ["provider_a", "provider_b"]
    assert [a.provider_name for a in response.attempts] == ["provider_a", "provider_b"]
    assert response.attempts[0].success is False
    assert response.attempts[1].success is True


def test_fallback_tries_second_provider_on_rate_limit() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 429)})

    response = router.invoke("hi")

    assert response.provider_name == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_not_found() -> None:
    """A 404 is provider-specific (typo'd base_url, model not hosted) -- fall back."""
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 404)})

    response = router.invoke("hi")

    assert response.provider_name == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_http_408_timeout() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 408)})

    response = router.invoke("hi")

    assert response.provider_name == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_server_error() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 503)})

    response = router.invoke("hi")

    assert response.provider_name == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_fallback_tries_second_provider_on_unknown_error() -> None:
    connect_error = ProviderConnectionError(
        "could not connect", provider_name="provider_a", model="model-a"
    )
    providers = [_config("provider_a"), _config("provider_b")]
    router, invoked = _router(providers, {"provider_a": connect_error})

    response = router.invoke("hi")

    assert response.provider_name == "provider_b"
    assert invoked == ["provider_a", "provider_b"]


def test_auth_error_disables_provider_for_current_run() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, _ = _router(providers, {"provider_a": _http("provider_a", 401)})

    first = router.invoke("hi")
    assert first.provider_name == "provider_b"
    assert first.attempts[0].provider_name == "provider_a"
    assert first.attempts[0].success is False

    # provider_a is now benched for the run: excluded (not re-tried) next call.
    second = router.invoke("hi")
    assert second.provider_name == "provider_b"
    excluded = {result.provider_name: result.reason for result in second.excluded}
    assert excluded["provider_a"] is FilterReason.AUTH_DISABLED_THIS_RUN


def test_bad_request_stops_immediately_without_trying_more_providers() -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    router, invoked = _router(providers, {"provider_a": _http("provider_a", 400)})

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke("hi")

    assert invoked == ["provider_a"]  # provider_b and provider_c never tried
    assert [a.provider_name for a in exc_info.value.attempts] == ["provider_a"]


def test_bad_request_after_earlier_failure_keeps_both_attempts() -> None:
    providers = [_config("provider_a"), _config("provider_b"), _config("provider_c")]
    router, invoked = _router(
        providers,
        {"provider_a": _timeout("provider_a"), "provider_b": _http("provider_b", 400)},
    )

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke("hi")

    assert invoked == ["provider_a", "provider_b"]  # provider_c never tried
    assert [a.provider_name for a in exc_info.value.attempts] == ["provider_a", "provider_b"]


def test_all_providers_fail_raises_with_each_distinct_reason() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    router, _ = _router(
        providers,
        {"provider_a": _timeout("provider_a"), "provider_b": _http("provider_b", 429)},
    )

    with pytest.raises(RouterExhaustedError) as exc_info:
        router.invoke("hi")

    message = str(exc_info.value)
    assert "provider_a" in message
    assert "provider_b" in message
    assert "timed out" in message
    assert "429" in message
    assert [a.provider_name for a in exc_info.value.attempts] == ["provider_a", "provider_b"]


def test_fallback_response_is_json_serializable() -> None:
    """A live exception in attempts must not break model_dump_json()."""
    providers = [_config("provider_a"), _config("provider_b")]
    router, _ = _router(providers, {"provider_a": _timeout("provider_a")})

    response = router.invoke("hi")
    dumped = response.model_dump_json()

    assert "ProviderTimeoutError" in dumped
    assert response.attempts[0].error is not None  # real object still on the model


def test_successful_fallback_records_real_unwrapped_error_for_failed_attempt() -> None:
    providers = [_config("provider_a"), _config("provider_b")]
    timeout = _timeout("provider_a")
    router, _ = _router(providers, {"provider_a": timeout})

    response = router.invoke("hi")

    assert response.attempts[0].success is False
    assert response.attempts[0].error is timeout  # exact, unwrapped exception object
    assert response.attempts[1].success is True
    assert response.attempts[1].error is None
