from __future__ import annotations


def resolve_provider_name(*, provider: str | None, base_url: str | None, model: str | None) -> str:
    """Resolve the provider name from explicit config or base_url heuristics.

    When *provider* is set to a known value (``"openai"``, ``"deepseek"``,
    ``"openai-compatible"``), it is returned as-is.  When *provider* is ``"auto"``
    (the default), the function uses a **best-effort heuristic** on *base_url* to
    guess the provider.  This heuristic is intentionally simple — substring
    matching against ``"api.openai.com"`` and ``"deepseek"`` — and may misclassify
    custom gateways whose hostname happens to contain those strings.  For
    precision, set ``HARNESS_PROVIDER`` explicitly.
    """
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
