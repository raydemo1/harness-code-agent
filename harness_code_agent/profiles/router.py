"""First-task profile routing.

The router is intentionally separate from the real agent conversation: it may
call an LLM, but its messages are never inserted into the bound profile slot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..agent.providers import ProviderAdapter, get_client
from . import list_profiles


DEFAULT_PROFILE = "coding-agent"
MIN_CONFIDENCE = 0.70


@dataclass(frozen=True)
class RouteDecision:
    profile_name: str
    confidence: float
    reason: str
    fallback_used: bool = False
    fallback_reason: str = ""


def route_profile_for_task(
    user_prompt: str,
    *,
    workspace: str | Path,
    confidence_threshold: float = MIN_CONFIDENCE,
) -> RouteDecision:
    """Choose the best profile for the first real user task.

    Invalid model output, low confidence, unknown profiles, and provider
    failures all fall back to the product default.
    """
    try:
        profiles = list_profiles()
        valid_profiles = {item["name"] for item in profiles}
        raw = _call_router_llm(
            user_prompt=user_prompt,
            workspace=Path(workspace),
            profiles=profiles,
        )
        data = _parse_router_json(raw)
        profile_name = str(data.get("profile_name") or "").strip()
        confidence = float(data.get("confidence") or 0.0)
        reason = str(data.get("reason") or "").strip()
        if profile_name not in valid_profiles:
            return _fallback(f"unknown profile: {profile_name or '<empty>'}")
        if confidence < confidence_threshold:
            return _fallback(f"low confidence: {confidence:.2f}", router_reason=reason)
        return RouteDecision(
            profile_name=profile_name,
            confidence=confidence,
            reason=reason or "Router selected this profile for the first task.",
        )
    except Exception as exc:
        return _fallback(f"router failed: {exc}")


def _call_router_llm(*, user_prompt: str, workspace: Path, profiles: list[dict[str, str]]) -> str:
    profile_lines = "\n".join(
        f"- {item['name']}: {item['description']}" for item in profiles
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You route the first real user task to one Harness profile. "
                "Return only compact JSON with keys: profile_name, confidence, reason. "
                "confidence must be a number from 0 to 1."
            ),
        },
        {
            "role": "user",
            "content": (
                "Available profiles:\n"
                f"{profile_lines}\n\n"
                "Workspace summary:\n"
                f"{_workspace_summary(workspace)}\n\n"
                "First user task:\n"
                f"{user_prompt}"
            ),
        },
    ]
    profile = config.resolve_model_profile("fast")
    adapter = ProviderAdapter(profile.provider)
    response = get_client().chat.completions.create(**adapter.chat_kwargs(
        profile=profile,
        messages=messages,
        max_tokens=512,
    ))
    return response.choices[0].message.content or ""


def _parse_router_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


def _workspace_summary(workspace: Path) -> str:
    try:
        entries = sorted(workspace.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        return f"workspace unreadable: {exc}"
    names = [item.name + ("/" if item.is_dir() else "") for item in entries[:40]]
    manifests = [
        name
        for name in [
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "README.md",
            "requirements.txt",
        ]
        if (workspace / name).exists()
    ]
    return "\n".join([
        f"path: {workspace}",
        "top-level: " + (", ".join(names) if names else "<empty>"),
        "manifests: " + (", ".join(manifests) if manifests else "<none>"),
    ])


def _fallback(fallback_reason: str, *, router_reason: str = "") -> RouteDecision:
    explanation = router_reason or "Using the default profile."
    return RouteDecision(
        profile_name=DEFAULT_PROFILE,
        confidence=0.0,
        reason=explanation,
        fallback_used=True,
        fallback_reason=fallback_reason,
    )
