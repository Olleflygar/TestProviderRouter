from __future__ import annotations

from nygen_router.config import ProviderConfig
from nygen_router.types import FilterReason, RouterRequest


def missing_capability(
    config: ProviderConfig, request: RouterRequest
) -> tuple[FilterReason, str] | None:
    """Return the first capability the request needs but the provider lacks.

    Returns ``None`` when the provider declares every capability the request
    requires. Used as a hard filter (PR2): a provider missing a required
    capability is excluded, never ranked lower.
    """
    capabilities = config.capabilities
    if request.requires_tools and not capabilities.supports_tools:
        return FilterReason.MISSING_TOOLS, "missing tool-calling support"
    if request.requires_streaming and not capabilities.supports_streaming:
        return FilterReason.MISSING_STREAMING, "missing streaming support"
    if request.requires_json_mode and not capabilities.supports_json_mode:
        return FilterReason.MISSING_JSON_MODE, "missing JSON-mode support"
    return None
