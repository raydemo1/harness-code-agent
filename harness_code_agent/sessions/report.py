from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any


def build_final_report(
    metadata: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    status: str,
    reason: str,
    summary: str = "",
) -> dict[str, Any]:
    """Build the phase-two final report payload from persisted session data."""
    metadata = metadata or {}
    events = events or []
    return {
        "session_id": str(metadata.get("id", "")),
        "status": status,
        "reason": reason,
        "summary": _report_summary(summary, events),
        "statistics": {
            "events": len(events),
            "user_inputs": _count_events(events, "user_input"),
            "assistant_messages": _count_events(events, "assistant_message"),
            "tool_calls": _tool_result_count(events),
            "failures": _count_events(events, "failure"),
            "file_changes": len(_changed_files(events)),
        },
        "failure_categories": dict(_failure_categories(events)),
        "tool_counts": dict(_tool_counts(events)),
        "changed_files": _changed_files(events),
        "started_at": str(metadata.get("created_at", "")),
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }


def _count_events(events: list[dict[str, Any]], event_type: str) -> int:
    return sum(1 for event in events if _event_type(event) == event_type)


def _tool_result_count(events: list[dict[str, Any]]) -> int:
    return _count_events(events, "tool_result")


def _tool_counts(events: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for event in events:
        if _event_type(event) != "tool_result":
            continue
        tool = _payload(event).get("tool") or "unknown"
        counts[str(tool)] += 1
    return counts


def _failure_categories(events: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for event in events:
        if _event_type(event) != "failure":
            continue
        category = _payload(event).get("category") or "unknown"
        counts[str(category)] += 1
    return counts


def _changed_files(events: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for event in events:
        if _event_type(event) != "file_change":
            continue
        path = _payload(event).get("path")
        if path is None:
            continue
        text = str(path)
        if text and text not in seen:
            seen.add(text)
            paths.append(text)
    return paths


def _report_summary(summary: str, events: list[dict[str, Any]]) -> str:
    text = (summary or "").strip()
    if not text:
        text = _latest_assistant_message(events)
    if not text:
        return "No assistant response recorded."
    text = " ".join(text.split())
    if len(text) > 500:
        return text[:497] + "..."
    return text


def _latest_assistant_message(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if _event_type(event) == "assistant_message":
            text = _payload(event).get("text")
            if text:
                return str(text)
    return ""


def _event_type(event: dict[str, Any]) -> str:
    return str((event or {}).get("type", ""))


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = (event or {}).get("payload")
    return payload if isinstance(payload, dict) else {}
