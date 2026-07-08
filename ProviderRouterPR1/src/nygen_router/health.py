from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProviderHealthState:
    """Per-run health state for a single provider, held on the router.

    Lives on ProviderRouter (not inside a policy) so the same state is visible
    to the eligibility filter and to any policy, and is not lost if the policy
    is swapped. PR5 extends this in place with cooldowns and consecutive-
    failure counts.
    """

    auth_disabled: bool = False
