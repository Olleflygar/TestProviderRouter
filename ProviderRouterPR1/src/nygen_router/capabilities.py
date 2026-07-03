from __future__ import annotations

from nygen_router.config import ProviderConfig
from nygen_router.errors import ConfigError
from nygen_router.types import RouterRequest


def validate_request_capabilities(config: ProviderConfig, request: RouterRequest) -> None:
    if request.requires_tools and not config.capabilities.supports_tools:
        raise ConfigError(f"Provider {config.name!r} does not support tool calls.")
    if request.requires_streaming and not config.capabilities.supports_streaming:
        raise ConfigError(f"Provider {config.name!r} does not support streaming.")
    if request.requires_json_mode and not config.capabilities.supports_json_mode:
        raise ConfigError(f"Provider {config.name!r} does not support JSON mode.")
