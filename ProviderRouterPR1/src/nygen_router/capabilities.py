from __future__ import annotations

from nygen_router.config import ProviderConfig
from nygen_router.errors import CapabilityError
from nygen_router.types import RouterRequest


def validate_request_capabilities(config: ProviderConfig, request: RouterRequest) -> None:
    """Raise CapabilityError if the provider can't meet a capability the request needs."""
    if request.requires_tools and not config.capabilities.supports_tools:
        raise CapabilityError(config.name, "tool calls")
    if request.requires_streaming and not config.capabilities.supports_streaming:
        raise CapabilityError(config.name, "streaming")
    if request.requires_json_mode and not config.capabilities.supports_json_mode:
        raise CapabilityError(config.name, "JSON mode")
