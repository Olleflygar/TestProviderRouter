from __future__ import annotations

from nygen_router.adapters.base import NormalizedStream
from nygen_router.config import ApiProtocol, ProviderCapabilities, ProviderConfig
from nygen_router.errors import (
    ConfigError,
    DuplicateCallVariantProtocolError,
    InvalidOperationArgumentsError,
    MissingApiKeyError,
    MixedCallTypeError,
    ModelArgumentConflictError,
    NoEligibleProvidersError,
    NoProvidersConfiguredError,
    NygenRouterError,
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderResponsesError,
    ProviderSDKNotInstalledError,
    ProviderStreamInterruptedError,
    ProviderTimeoutError,
    RouterExhaustedError,
    UnsupportedOperationError,
    UnsupportedProtocolError,
)
from nygen_router.filters import filter_eligible_providers
from nygen_router.health import HealthConfig, ProviderHealthReport
from nygen_router.metrics import MetricsEvent
from nygen_router.policies import (
    HistoryScope,
    Policy,
    RoundRobinPolicy,
    RoutingContext,
    ScoreBasedPolicy,
    StickyRoutingPolicy,
)
from nygen_router.router import ProviderRouter, StreamFailurePolicy, StreamRestart
from nygen_router.scoring import ProviderScore, ScoreWeights, calculate_provider_score
from nygen_router.stats import ProviderStats, aggregate_stats
from nygen_router.storage import DuckDBMetricsStore, MetricsStore, SQLiteMetricsStore
from nygen_router.types import (
    CallType,
    CallVariant,
    EligibilityResult,
    FilterReason,
    ProviderAttempt,
)

__all__ = [
    "ApiProtocol",
    "CallVariant",
    "CallType",
    "ConfigError",
    "DuckDBMetricsStore",
    "DuplicateCallVariantProtocolError",
    "EligibilityResult",
    "FilterReason",
    "HealthConfig",
    "HistoryScope",
    "InvalidOperationArgumentsError",
    "MetricsEvent",
    "MetricsStore",
    "MixedCallTypeError",
    "MissingApiKeyError",
    "ModelArgumentConflictError",
    "NoEligibleProvidersError",
    "NoProvidersConfiguredError",
    "NormalizedStream",
    "NygenRouterError",
    "Policy",
    "ProviderAttempt",
    "ProviderCapabilities",
    "ProviderConfig",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderResponsesError",
    "ProviderHealthReport",
    "ProviderRouter",
    "ProviderScore",
    "ProviderSDKNotInstalledError",
    "ProviderStats",
    "ProviderStreamInterruptedError",
    "ProviderTimeoutError",
    "RoundRobinPolicy",
    "RoutingContext",
    "RouterExhaustedError",
    "ScoreBasedPolicy",
    "ScoreWeights",
    "SQLiteMetricsStore",
    "StreamFailurePolicy",
    "StreamRestart",
    "StickyRoutingPolicy",
    "UnsupportedOperationError",
    "UnsupportedProtocolError",
    "aggregate_stats",
    "calculate_provider_score",
    "filter_eligible_providers",
]
