"""Shared utilities for the agent package."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from .. import config

if TYPE_CHECKING:
    from .conversation import Agent


def _get(value, key: str, default=None):
    """Get an attribute/key from either an object or a dict."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _usage_to_dict(usage) -> dict | None:
    """Convert an OpenAI Usage object (or dict) to a plain dict."""
    if usage is None:
        return None
    prompt_tokens = _get(usage, "prompt_tokens")
    completion_tokens = _get(usage, "completion_tokens")
    total_tokens = _get(usage, "total_tokens")
    details = _get(usage, "prompt_tokens_details") or {}
    cached_tokens = _get(details, "cached_tokens", 0) or 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
    }


def _prompt_cache_key(agent: Agent, tool_schemas: list[dict] | None) -> str:
    tool_text = json.dumps(
        _canonical_tool_schemas(tool_schemas or []),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    stable_prefix = getattr(agent, "prompt_cache_identity", None) or {
        "system_prompt_hash": _short_hash(agent.system_prompt),
    }
    payload = {
        "agent": agent.name,
        "model": config.MODEL,
        "stable_prefix": stable_prefix,
        "tools_hash": _short_hash(tool_text),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "hca:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:48]


def _canonical_tool_schemas(tool_schemas: list[dict]) -> list[dict]:
    return sorted(
        (_canonical_schema_value(schema) for schema in tool_schemas),
        key=lambda schema: str(schema.get("function", {}).get("name") or ""),
    )


def _canonical_schema_value(value, *, parent_key: str = ""):
    if isinstance(value, dict):
        return {
            key: _canonical_schema_value(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        items = [_canonical_schema_value(item, parent_key=parent_key) for item in value]
        if parent_key == "required" and all(isinstance(item, str) for item in items):
            return sorted(items)
        return items
    return value


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
