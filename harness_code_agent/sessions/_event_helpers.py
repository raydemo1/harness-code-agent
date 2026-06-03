from __future__ import annotations

from collections import Counter
from typing import Any


def count_events(events: list[dict[str, Any]], event_type_name: str) -> int:
    return sum(1 for event in events if event_type(event) == event_type_name)


def tool_counts(events: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for event in events:
        if event_type(event) != "tool_result":
            continue
        tool = payload(event).get("tool") or "unknown"
        counts[str(tool)] += 1
    return counts


def failure_categories(events: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for event in events:
        if event_type(event) != "failure":
            continue
        category = payload(event).get("category") or "unknown"
        counts[str(category)] += 1
    return counts


def changed_files(events: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for event in events:
        if event_type(event) != "file_change":
            continue
        path = payload(event).get("path")
        if path is None:
            continue
        text = str(path)
        if is_ignored_changed_file(text):
            continue
        if text and text not in seen:
            seen.add(text)
            paths.append(text)
    return paths


def is_ignored_changed_file(path: str) -> bool:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    lowered = text.lower()
    if not lowered:
        return False
    ignored_prefixes = (
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        "htmlcov/",
        "node_modules/.cache/",
        "dist/",
        "build/",
        "__pycache__/",
    )
    if lowered in {".coverage"}:
        return True
    if lowered.endswith(".pyc"):
        return True
    if "/__pycache__/" in lowered:
        return True
    return any(lowered.startswith(prefix) for prefix in ignored_prefixes)


def event_type(event: dict[str, Any]) -> str:
    return str((event or {}).get("type", ""))


def payload(event: dict[str, Any]) -> dict[str, Any]:
    event_payload = (event or {}).get("payload")
    return event_payload if isinstance(event_payload, dict) else {}
