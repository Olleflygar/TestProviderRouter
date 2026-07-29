from __future__ import annotations

import logging
from typing import Any

from nygen_router.adapters.base import NormalizedStream
from nygen_router.adapters.openai_compatible import (
    OpenAICompatibleAdapter,
    _map_stream_exception,
)
from nygen_router.errors import ProviderError, ProviderResponsesError

logger = logging.getLogger(__name__)


class OpenAIResponsesAdapter(OpenAICompatibleAdapter):
    """Dispatch native ``responses.create`` calls through the OpenAI SDK."""

    def _wrap_stream(self, stream: Any) -> NormalizedStream:
        return OpenAIResponsesStream(
            stream,
            provider_name=self.config.name,
            model=self.config.model,
            timeout_seconds=self.config.timeout_seconds,
        )

    def _handle_response(self, response: Any) -> Any:
        status = getattr(response, "status", None)
        if status == "incomplete":
            _log_incomplete(response, provider_name=self.config.name, model=self.config.model)
        elif status == "failed":
            raise _failure_error(
                response=response,
                provider_name=self.config.name,
                model=self.config.model,
            )
        return response


class OpenAIResponsesStream(NormalizedStream):
    """Yield native Responses streaming events while observing terminal state."""

    def __init__(
        self,
        stream: Any,
        *,
        provider_name: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._stream = stream
        self._provider_name = provider_name
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._completed = False
        self._recognized = False
        self._usage: Any = None
        self._incomplete_warning_emitted = False

    def __next__(self) -> Any:
        try:
            event = next(self._stream)
        except StopIteration:
            raise
        except Exception as exc:
            raise self._as_router_error(exc) from exc

        event_type = getattr(event, "type", None)
        if not isinstance(event_type, str) or not (
            event_type == "error" or event_type.startswith("response.")
        ):
            return event

        self._recognized = True
        if event_type == "response.completed":
            self._observe_terminal_response(getattr(event, "response", None))
        elif event_type == "response.incomplete":
            response = getattr(event, "response", None)
            self._observe_terminal_response(response)
            if not self._incomplete_warning_emitted:
                _log_incomplete(
                    response,
                    provider_name=self._provider_name,
                    model=self._model,
                )
                self._incomplete_warning_emitted = True
        elif event_type in {"response.failed", "error"}:
            raise _failure_error(
                event=event,
                response=getattr(event, "response", None),
                provider_name=self._provider_name,
                model=self._model,
            )
        return event

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def recognized(self) -> bool:
        return self._recognized

    @property
    def usage(self) -> Any:
        return self._usage

    def close(self) -> None:
        self._stream.close()

    def _observe_terminal_response(self, response: Any) -> None:
        self._completed = True
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._usage = usage

    def _as_router_error(self, exc: Exception) -> ProviderError:
        return _map_stream_exception(
            exc,
            provider_name=self._provider_name,
            model=self._model,
            timeout_seconds=self._timeout_seconds,
        )


def _log_incomplete(response: Any, *, provider_name: str, model: str) -> None:
    response_id = getattr(response, "id", None)
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None)
    logger.warning(
        "Provider %r returned an incomplete response for model %r "
        "(response_id=%r, reason=%r); the result is served without fallback or benching.",
        provider_name,
        model,
        response_id,
        reason,
    )


def _failure_error(
    *,
    provider_name: str,
    model: str,
    event: Any = None,
    response: Any = None,
) -> ProviderResponsesError:
    embedded_error = getattr(response, "error", None)
    event_code = getattr(event, "code", None)
    event_message = getattr(event, "message", None)
    event_param = getattr(event, "param", None)
    code = event_code if isinstance(event_code, str) else getattr(embedded_error, "code", None)
    message = (
        event_message
        if isinstance(event_message, str)
        else getattr(embedded_error, "message", None)
    )
    if not isinstance(message, str):
        response_id = getattr(response, "id", None)
        message = f"Responses API declared response {response_id!r} failed."
    param = event_param if isinstance(event_param, str) else getattr(embedded_error, "param", None)
    return ProviderResponsesError(
        provider_name=provider_name,
        model=model,
        message=message,
        error_code=code if isinstance(code, str) else None,
        param=param if isinstance(param, str) else None,
        event=event,
        response=response,
    )
