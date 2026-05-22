from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionStatusSnapshot:
    profile: str
    model: str
    provider: str
    permission_mode: str
    session_id: str
    cwd: Path
    turn: int = 0
    pending_plan: bool = False
    checkpoint: str = ""
    running_tool: str = ""
    status: str = "idle"
    dirty_count: int = 0


@dataclass
class TranscriptBlock:
    kind: str
    title: str
    body: str = ""
    status: str = ""


@dataclass
class TuiState:
    snapshot: SessionStatusSnapshot
    blocks: list[TranscriptBlock] = field(default_factory=list)
    pending_approval: dict[str, Any] | None = None

    def apply_event(self, event: Any) -> TranscriptBlock | None:
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_type = str(data.get("type", ""))
        payload = data.get("payload") or {}

        if event_type == "session_started":
            self.snapshot.status = "ready"
            self.snapshot.profile = str(payload.get("profile") or self.snapshot.profile)
            return TranscriptBlock("session", "session started", _payload_summary(payload), "success")
        if event_type == "user_input":
            self.snapshot.turn = int(payload.get("turn") or self.snapshot.turn)
            return TranscriptBlock("user", f"user turn {self.snapshot.turn}", str(payload.get("text", "")))
        if event_type == "turn_started":
            self.snapshot.status = "running"
            self.snapshot.turn = int(payload.get("turn") or self.snapshot.turn)
            return TranscriptBlock("status", f"turn {self.snapshot.turn} started", _payload_summary(payload))
        if event_type == "assistant_message":
            self.snapshot.status = "idle"
            return TranscriptBlock("assistant", "assistant", str(payload.get("text", "")))
        if event_type == "tool_call":
            tool = str(payload.get("tool", "tool"))
            self.snapshot.running_tool = tool
            self.snapshot.status = "tool"
            return TranscriptBlock("tool", f"tool call: {tool}", _summarize_tool_args(payload.get("args")), "running")
        if event_type == "tool_result":
            tool = str(payload.get("tool", "tool"))
            if self.snapshot.running_tool == tool:
                self.snapshot.running_tool = ""
            status = str(payload.get("status", "unknown"))
            self.snapshot.status = "running"
            body = str(payload.get("error") or payload.get("output") or "")
            return TranscriptBlock("tool", f"tool result: {tool}", _tail(body), status)
        if event_type == "file_change":
            self.snapshot.dirty_count += 1
            return TranscriptBlock("file", "file changed", _payload_summary(payload), "changed")
        if event_type == "failure":
            self.snapshot.status = "needs attention"
            return TranscriptBlock("failure", "failure", _payload_summary(payload), "failed")
        if event_type == "approval_requested":
            self.pending_approval = payload
            return TranscriptBlock("approval", "approval requested", _payload_summary(payload), "pending")
        if event_type == "approval_decided":
            self.pending_approval = None
            status = "approved" if payload.get("approved") else "denied"
            return TranscriptBlock("approval", f"approval {status}", _payload_summary(payload), status)
        if event_type == "profile_switched":
            self.snapshot.profile = str(payload.get("profile") or self.snapshot.profile)
            self.snapshot.pending_plan = False
            previous = payload.get("previous_profile", "")
            current = payload.get("profile", "")
            reason = payload.get("reason", "")
            return TranscriptBlock("profile", "profile switched", f"{previous} -> {current} ({reason})")
        if event_type == "plan_ready":
            self.snapshot.pending_plan = True
            self.snapshot.status = "plan ready"
            path = str(payload.get("plan_path") or "global_plan/current/plan.md")
            revision = payload.get("plan_revision")
            suffix = f" rev {revision}" if revision is not None else ""
            return TranscriptBlock(
                "plan",
                "plan ready",
                f"{path}{suffix}\n[执行计划]  [修改计划: 输入修改理由或补充要求]",
                "pending",
            )
        if event_type == "turn_finished":
            self.snapshot.status = "idle"
            self.snapshot.running_tool = ""
            self.snapshot.checkpoint = str(payload.get("checkpoint") or "")
            return TranscriptBlock("status", f"turn {payload.get('turn', self.snapshot.turn)} finished", self.snapshot.checkpoint)
        if event_type == "session_finished":
            self.snapshot.status = str(payload.get("status") or "closed")
            return TranscriptBlock("session", "session finished", _payload_summary(payload))
        return None

    def add_block(self, block: TranscriptBlock | None) -> None:
        if block is not None:
            self.blocks.append(block)


def _payload_summary(payload: dict[str, Any]) -> str:
    parts = []
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, dict):
            value = _payload_summary(value)
        text = str(value).replace("\n", " ")
        if len(text) > 160:
            text = text[:157] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def _summarize_tool_args(args: Any) -> str:
    if not isinstance(args, dict):
        return str(args or "")
    clean = {}
    for key, value in args.items():
        if key == "content":
            clean[key] = f"[{len(str(value))} chars]"
        else:
            clean[key] = value
    return _payload_summary(clean)


def _tail(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "...[truncated]\n" + text[-limit:]
