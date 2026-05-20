from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SessionEvent:
    sequence: int
    timestamp: float
    type: str
    agent: str | None
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": self.type,
            "agent": self.agent,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class StructuredEvent:
    type: str
    payload: dict[str, Any]
    agent: str | None = None

    def to_event(self) -> SessionEvent:
        return SessionEvent(
            sequence=0,
            timestamp=0.0,
            type=self.type,
            agent=self.agent,
            payload=self.payload,
        )


@dataclass(frozen=True)
class UserInputEvent:
    text: str
    turn: int | None = None
    mentions: list[str] | None = None
    agent: str | None = "main_agent"

    def to_event(self) -> StructuredEvent:
        payload: dict[str, Any] = {"text": self.text}
        if self.turn is not None:
            payload["turn"] = self.turn
        if self.mentions is not None:
            payload["mentions"] = self.mentions
        return StructuredEvent("user_input", payload, self.agent)


@dataclass(frozen=True)
class AssistantMessageEvent:
    text: str
    turn: int | None = None
    agent: str | None = "main_agent"

    def to_event(self) -> StructuredEvent:
        payload: dict[str, Any] = {"text": self.text}
        if self.turn is not None:
            payload["turn"] = self.turn
        return StructuredEvent("assistant_message", payload, self.agent)


@dataclass(frozen=True)
class ToolCallEvent:
    tool: str
    args: dict[str, Any]
    agent: str | None = "main_agent"

    def to_event(self) -> StructuredEvent:
        return StructuredEvent("tool_call", {"tool": self.tool, "args": self.args}, self.agent)


@dataclass(frozen=True)
class ToolResultEvent:
    tool: str
    status: str | None = None
    output: str = ""
    error: str | None = None
    return_code: int | None = None
    metadata: dict[str, Any] | None = None
    ok: bool | None = None
    agent: str | None = "main_agent"

    def to_event(self) -> StructuredEvent:
        status = self.status or _tool_status_from_ok(self.ok)
        payload: dict[str, Any] = {
            "tool": self.tool,
            "status": status,
            "ok": _ok_from_tool_status(status),
            "output": self.output,
        }
        if self.error is not None:
            payload["error"] = self.error
        if self.return_code is not None:
            payload["return_code"] = self.return_code
        if self.metadata:
            payload["metadata"] = self.metadata
        return StructuredEvent("tool_result", payload, self.agent)


@dataclass(frozen=True)
class FileChangeEvent:
    path: str
    operation: str | None = None
    snapshot_path: str | None = None
    agent: str | None = "main_agent"

    def to_event(self) -> StructuredEvent:
        payload: dict[str, Any] = {"path": self.path}
        if self.operation:
            payload["operation"] = self.operation
        if self.snapshot_path:
            payload["snapshot_path"] = self.snapshot_path
        return StructuredEvent("file_change", payload, self.agent)


@dataclass(frozen=True)
class FailureEvent:
    category: str
    message: str
    agent: str | None = "main_agent"

    def to_event(self) -> StructuredEvent:
        return StructuredEvent("failure", {"category": self.category, "message": self.message}, self.agent)


@dataclass(frozen=True)
class SessionFinishedEvent:
    reason: str
    status: str
    agent: str | None = "main_agent"

    def to_event(self) -> StructuredEvent:
        return StructuredEvent(
            "session_finished",
            {"reason": self.reason, "status": self.status},
            self.agent,
        )


@dataclass(frozen=True)
class TaskOutcomeEvent:
    status: str
    evidence: list[str]
    summary: str
    agent: str | None = "main_agent"

    def to_event(self) -> StructuredEvent:
        return StructuredEvent(
            "task_outcome",
            {
                "status": self.status,
                "evidence": self.evidence,
                "summary": self.summary,
            },
            self.agent,
        )


class EventBus:
    """Append-only event stream for product runtime observability."""

    def __init__(self, events_path: str | Path | None = None):
        self.events_path = Path(events_path) if events_path is not None else None
        self.events: list[SessionEvent] = []
        self._sequence = 0
        if self.events_path is not None:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        event_type: str,
        *,
        agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SessionEvent:
        self._sequence += 1
        event = SessionEvent(
            sequence=self._sequence,
            timestamp=time.time(),
            type=event_type,
            agent=agent,
            payload=payload or {},
        )
        self.events.append(event)
        if self.events_path is not None:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return event

    def emit_event(self, structured_event: StructuredEvent) -> SessionEvent:
        event = structured_event.to_event()
        return self.emit(
            event.type,
            agent=event.agent,
            payload=event.payload,
        )


def _tool_status_from_ok(ok: bool | None) -> str:
    if ok is True:
        return "success"
    if ok is False:
        return "failed"
    return "unknown"


def _ok_from_tool_status(status: str) -> bool | None:
    if status == "success":
        return True
    if status == "failed":
        return False
    return None
