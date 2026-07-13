from __future__ import annotations

from nygen_router.config import ApiProtocol, ProviderCapabilities, ProviderConfig
from nygen_router.errors import (
    ConfigError,
    DuplicateCallVariantProtocolError,
    InvalidOperationArgumentsError,
    MissingApiKeyError,
    ModelArgumentConflictError,
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
    NygenRouterError,
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderSDKNotInstalledError,
    ProviderTimeoutError,
    RouterExhaustedError,
    UnsupportedOperationError,
    UnsupportedProtocolError,
)
from nygen_router.filters import filter_eligible_providers
from nygen_router.policies import RoundRobinPolicy
from nygen_router.router import ProviderRouter
from nygen_router.types import (
    CallVariant,
    EligibilityResult,
    FilterReason,
    ProviderAttempt,
)

__all__ = [
    "ApiProtocol",
    "CallVariant",
    "ConfigError",
    "DuplicateCallVariantProtocolError",
    "EligibilityResult",
    "FilterReason",
    "InvalidOperationArgumentsError",
    "MissingApiKeyError",
    "ModelArgumentConflictError",
    "NoEligibleProvidersError",
    "NoProvidersConfiguredError",
    "NygenRouterError",
    "ProviderAttempt",
    "ProviderCapabilities",
    "ProviderConfig",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderRouter",
    "ProviderSDKNotInstalledError",
    "ProviderTimeoutError",
    "RoundRobinPolicy",
    "RouterExhaustedError",
    "UnsupportedOperationError",
    "UnsupportedProtocolError",
    "filter_eligible_providers",
]
