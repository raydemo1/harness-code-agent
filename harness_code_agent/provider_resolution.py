from __future__ import annotations


def resolve_provider_name(*, provider: str | None, base_url: str | None, model: str | None) -> str:
    requested = (provider or "auto").strip().lower()
    if requested in {"openai-compatible", "compatible"}:
        return "openai-compatible"
    if requested in {"openai", "deepseek"}:
        return requested
    if requested != "auto":
        raise ValueError(f"Unsupported HARNESS_PROVIDER: {provider}")

    base_signature = (base_url or "").lower()
    if "api.openai.com" in base_signature:
        return "openai"
    if "deepseek" in base_signature:
        return "deepseek"
    return "openai-compatible"
