from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompt_toolkit.formatted_text import StyleAndTextTuples


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
    context_tokens: int = 0
    context_window_tokens: int = 0
    context_observe_threshold: int = 0
    context_prepare_threshold: int = 0
    context_allow_threshold: int = 0
    context_force_threshold: int = 0
    context_hint: bool = False


@dataclass
class TranscriptBlock:
    kind: str
    title: str
    body: str = ""
    status: str = ""


@dataclass
class ToolSummary:
    tool: str
    status: str
    args_summary: str = ""
    output_chars: int = 0
    return_code: int | None = None
    error_summary: str = ""
    elapsed: float = 0.0


@dataclass
class TuiState:
    snapshot: SessionStatusSnapshot
    blocks: list[TranscriptBlock] = field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    transcript_fragments: StyleAndTextTuples = field(default_factory=list)
    _pending_tools: dict[str, float] = field(default_factory=dict)
    show_thought_details: bool = False

    def toggle_thought_details(self) -> None:
        self.show_thought_details = not self.show_thought_details

    def add_transcript_fragments(self, fragments: StyleAndTextTuples) -> None:
        self.transcript_fragments = list(self.transcript_fragments) + list(fragments)

    def add_block_fragments(self, block: TranscriptBlock | None) -> None:
        if block is None:
            return
        from .render import render_block_fragments
        fragments = render_block_fragments(block)
        self.add_transcript_fragments(fragments)

    def add_block_fragments_simple(self, kind: str, title: str, body: str) -> None:
        self.add_block_fragments(TranscriptBlock(kind, title, body))

    def append_streaming_text(self, text: str) -> None:
        """Append streaming text to the transcript (inline with last content)."""
        self.transcript_fragments = list(self.transcript_fragments) + [("", text)]

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
            self._pending_tools[tool] = time.time()
            args_summary = _summarize_tool_args(payload.get("args"))
            return TranscriptBlock("tool", f"{tool}({args_summary})", "", "running")
        if event_type == "tool_result":
            tool = str(payload.get("tool", "tool"))
            if self.snapshot.running_tool == tool:
                self.snapshot.running_tool = ""
            status = str(payload.get("status", "unknown"))
            self.snapshot.status = "running"
            # Calculate elapsed time
            start_time = self._pending_tools.pop(tool, None)
            elapsed = time.time() - start_time if start_time else 0.0
            # Build summary
            output = str(payload.get("output") or "")
            error = str(payload.get("error") or "")
            return_code = payload.get("return_code")
            summary = ToolSummary(
                tool=tool,
                status=status,
                output_chars=len(output),
                return_code=return_code,
                error_summary=_tail(error, 120) if error else "",
                elapsed=elapsed,
            )
            # Build summary body: size + error (no full output)
            parts = []
            if summary.elapsed > 0:
                parts.append(_format_elapsed(summary.elapsed))
            if summary.output_chars > 0:
                parts.append(_format_size(summary.output_chars))
            if summary.return_code is not None:
                parts.append(f"rc={summary.return_code}")
            body = "  ".join(parts)
            if summary.error_summary:
                body += f"\n{summary.error_summary}"
            return TranscriptBlock("tool", tool, body, status)
        if event_type == "file_change":
            self.snapshot.dirty_count += 1
            return TranscriptBlock("file", "file changed", _payload_summary(payload), "changed")
        if event_type == "failure":
            self.snapshot.status = "needs attention"
            return TranscriptBlock("failure", "failure", _payload_summary(payload), "failed")
        if event_type == "approval_requested":
            self.pending_approval = payload
            return TranscriptBlock("approval", "approval requested", _approval_summary(payload), "pending")
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
        if event_type == "permission_mode_switched":
            self.snapshot.permission_mode = str(payload.get("permission_mode") or self.snapshot.permission_mode)
            previous = payload.get("previous_permission_mode", "")
            current = payload.get("permission_mode", "")
            return TranscriptBlock("status", "permission mode switched", f"{previous} -> {current}")
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
        if event_type == "context_compaction_started":
            forced = payload.get("forced", False)
            self.snapshot.status = "compacting (forced)" if forced else "compacting"
            label = "forced compaction started" if forced else "context compaction started"
            return TranscriptBlock("status", label, _payload_summary(payload), "running")
        if event_type == "context_compaction_committed":
            self.snapshot.status = "idle"
            tokens_saved = payload.get("tokens_saved", 0)
            body = f"tokens saved: {tokens_saved}" if tokens_saved else _payload_summary(payload)
            return TranscriptBlock("status", "context compacted", body, "success")
        if event_type == "context_compaction_failed":
            self.snapshot.status = "compaction failed"
            return TranscriptBlock("failure", "compaction failed", _payload_summary(payload), "failed")
        if event_type == "context_compaction_forced":
            self.snapshot.status = "compacting (forced)"
            return TranscriptBlock("status", "forced compaction", _payload_summary(payload), "running")
        if event_type == "turn_finished":
            self.snapshot.status = "idle"
            self.snapshot.running_tool = ""
            self.snapshot.checkpoint = str(payload.get("checkpoint") or "")
            return TranscriptBlock("status", f"turn {payload.get('turn', self.snapshot.turn)} finished", self.snapshot.checkpoint)
        if event_type == "session_finished":
            self.snapshot.status = str(payload.get("status") or "closed")
            return TranscriptBlock("session", "session finished", _payload_summary(payload))
        if event_type == "thought_started":
            self.snapshot.status = "thinking"
            return None
        if event_type == "thought_finished":
            duration = payload.get("duration_seconds", 0)
            truncated = payload.get("truncated", False)
            source = payload.get("source", "")
            body = f"thought for {_format_elapsed(duration)}"
            if self.show_thought_details:
                parts = [body]
                if source:
                    parts.append(f"source: {source}")
                if truncated:
                    parts.append("truncated: yes")
                body = "  ·  ".join(parts)
            return TranscriptBlock("thought", "thinking", body, "thought")
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


def _approval_summary(payload: dict[str, Any]) -> str:
    parts = []
    for key in ("tool", "risk", "reason"):
        if key in payload:
            text = str(payload[key]).replace("\n", " ")
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


def _format_elapsed(seconds: float) -> str:
    if seconds < 1.0:
        return f"{int(seconds * 1000)}ms"
    return f"{seconds:.1f}s"


def _format_size(chars: int) -> str:
    if chars < 1024:
        return f"{chars}B"
    return f"{chars / 1024:.1f}KB"
