from __future__ import annotations


# Stub; populated in a later step.
CAPABILITIES: dict[tuple[str, str], dict] = {
    # ("provider", "model_fragment"): {"tool_calling": bool, "json_mode": bool}
}


def supports(provider: str, model: str, capability: str) -> bool | None:
    """Return True/False if known, None if unknown."""
    provider = provider.lower()
    model = model.lower()
    for (candidate_provider, model_fragment), capabilities in CAPABILITIES.items():
        if candidate_provider == provider and model_fragment.lower() in model:
            value = capabilities.get(capability)
            return value if isinstance(value, bool) else None
    return None
