from __future__ import annotations

import json

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "jwt",
    "authorization",
    "credential",
    "private_key",
)


def safe_args_preview(tool_args: dict, max_chars: int = 200) -> str:
    """Return a sanitized, bounded preview of tool arguments."""
    safe: dict[str, object] = {}
    for key, value in dict(tool_args or {}).items():
        key_text = str(key)
        if _is_sensitive_key(key_text):
            safe[key_text] = "[redacted]"
            continue
        try:
            value_text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except TypeError:
            value_text = str(value)
        if key_text.lower() in {"content", "output", "text", "input", "code", "patch"} or len(value_text) > 120:
            safe[key_text] = f"[{len(value_text)} chars]"
        else:
            safe[key_text] = value
    try:
        preview = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        preview = str(safe)
    if len(preview) <= max_chars:
        return preview
    return preview[:max(0, max_chars)] + "..."


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)
