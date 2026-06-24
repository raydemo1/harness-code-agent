"""Environment forwarding helpers for Harbor-installed benchmark agents."""
from __future__ import annotations

import os


RUNNER_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "HARNESS_PROVIDER",
    "HARNESS_MODEL",
    "HARNESS_MODEL_INTENSITY",
    "HARNESS_MODEL_FAST",
    "HARNESS_MODEL_NORMAL",
    "HARNESS_MODEL_HARD",
    "HARNESS_MODEL_MAX",
    "MAX_AGENT_ITERATIONS",
    "MAX_AGENT_TOOL_CALLS",
    "MAX_AGENT_TOTAL_TOKENS",
    "AGENT_BUDGET_WARN_FRACTION",
)


def runner_env_vars() -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for key in RUNNER_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            env_vars[key] = val
    return env_vars
