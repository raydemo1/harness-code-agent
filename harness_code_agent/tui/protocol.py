"""Stable protocol contract shared by the Python bridge and OpenTUI client."""

from __future__ import annotations

from typing import Any

UI_PROTOCOL_VERSION = 4

_EVENT_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "snapshot": frozenset({"snapshot"}),
    "session_reset": frozenset({"snapshot"}),
    "transcript": frozenset({"item"}),
    "transcript_update": frozenset({"id", "body"}),
    "assistant_delta": frozenset({"id", "text"}),
    "commands": frozenset({"commands"}),
    "progress": frozenset({"status", "detail"}),
    "notice": frozenset({"text"}),
    "turn_state": frozenset({"state"}),
    "panel": frozenset({"panel"}),
    "interaction": frozenset({"id", "kind", "payload"}),
    "interaction_closed": frozenset({"id"}),
    "shutdown": frozenset(),
}


def validate_ui_event(event: dict[str, Any]) -> None:
    """Reject malformed runtime events before they cross the NDJSON boundary."""

    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in _EVENT_REQUIRED_FIELDS:
        raise ValueError(f"unsupported UI event type: {event_type!r}")
    missing = _EVENT_REQUIRED_FIELDS[event_type].difference(event)
    if missing:
        fields = ", ".join(sorted(missing))
        raise ValueError(f"UI event {event_type!r} is missing: {fields}")
