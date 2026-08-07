from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol


class ProviderAdapter(Protocol):
    def invoke(self, operation: str, arguments: dict[str, object]) -> Any:
        """Dispatch operation/arguments to one provider and return its native response."""
        ...


class NormalizedStream(ABC):
    """A provider stream the router can observe without interpreting its chunks.

    An ABC rather than a Protocol because the router detects streams with
    ``isinstance``: a structural check would also match any other iterable a
    custom adapter happens to return, and a raw SDK stream must keep passing
    through untouched. Returning one of these is the documented opt-in to
    mid-stream fallback.

    Stdlib imports only -- this module is reached by the bare
    ``from llm_provider_router import ProviderRouter`` import.
    """

    def __iter__(self) -> NormalizedStream:
        return self

    @abstractmethod
    def __next__(self) -> Any:
        """Yield the SDK's next raw chunk, unchanged.

        Any exception leaving here other than ``StopIteration`` must already be
        a router error with the SDK's own exception chained via
        ``raise ... from`` -- the router categorizes what it catches and never
        re-wraps it a second time.
        """

    @property
    @abstractmethod
    def completed(self) -> bool:
        """Whether a completion marker has been seen on this stream."""

    @property
    @abstractmethod
    def usage(self) -> Any:
        """The provider-reported usage object, or None if the stream carried none."""

    @property
    def recognized(self) -> bool:
        """Whether this wrapper understood the chunk shape it was given.

        False means ``completed`` carries no information -- the wrapper never
        saw a chunk it could read a completion marker from -- so the router
        must not call a clean end a silent truncation. Defaults to True: a
        wrapper that implements ``completed`` is asserting it can judge one.
        """
        return True

    @abstractmethod
    def close(self) -> None:
        """Stop the underlying stream and release its connection."""
