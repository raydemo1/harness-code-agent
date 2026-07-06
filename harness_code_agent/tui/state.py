from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionStatusSnapshot:
    profile: str
    model: str
    provider: str
    permission_mode: str
    session_id: str | None
    cwd: Path
    turn: int = 0
    pending_plan: bool = False
    checkpoint: str = ""
    running_tool: str = ""
    status: str = "idle"
    dirty_count: int = 0
    context_tokens: int = 0
    context_window_tokens: int = 0
    context_compact_threshold: int = 0
    context_hint: bool = False


@dataclass
class TranscriptBlock:
    kind: str
    title: str
    body: str = ""
    status: str = ""
    turn: int | None = None
    detail: bool = False


@dataclass(frozen=True)
class PlanStep:
    text: str
    status: str


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
    plan_steps: list[PlanStep] = field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    _pending_tools: dict[str, float] = field(default_factory=dict)
    show_thought_details: bool = False
    collapsed_turns: set[int] = field(default_factory=set)
    _latest_collapsed_turn: int | None = None

    def toggle_thought_details(self) -> None:
        self.show_thought_details = not self.show_thought_details

    def toggle_latest_turn_details(self) -> bool:
        if self._latest_collapsed_turn is None:
            return False
        turn = self._latest_collapsed_turn
        if turn in self.collapsed_turns:
            self.collapsed_turns.remove(turn)
        else:
            self.collapsed_turns.add(turn)
        return True

    def visible_blocks(self) -> list[TranscriptBlock]:
        return [
            block
            for block in self.blocks
            if not (
                block.detail
                and block.turn is not None
                and block.turn in self.collapsed_turns
            )
        ]

    def append_streaming_text(self, text: str) -> None:
        """Streaming text is rendered by TranscriptView as an active block."""
        return None

    def apply_event(self, event: Any) -> TranscriptBlock | None:
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_type = str(data.get("type", ""))
        payload = data.get("payload") or {}

        if event_type == "session_started":
            self.snapshot.status = "ready"
            self.snapshot.profile = str(payload.get("profile") or self.snapshot.profile)
            if payload.get("session_id"):
                self.snapshot.session_id = str(payload.get("session_id"))
            return TranscriptBlock("session", "session started", _payload_summary(payload), "success")
        if event_type == "user_input":
            self.snapshot.turn = int(payload.get("turn") or self.snapshot.turn)
            return TranscriptBlock("user", f"user turn {self.snapshot.turn}", str(payload.get("text", "")), turn=self.snapshot.turn)
        if event_type == "turn_started":
            self.snapshot.status = "running"
            self.snapshot.turn = int(payload.get("turn") or self.snapshot.turn)
            return TranscriptBlock("status", f"turn {self.snapshot.turn} started", _payload_summary(payload), turn=self.snapshot.turn, detail=True)
        if event_type == "assistant_message":
            self.snapshot.status = "idle"
            turn = _payload_turn(payload, self.snapshot.turn)
            return TranscriptBlock("assistant", "assistant", str(payload.get("text", "")), turn=turn)
        if event_type == "tool_call":
            tool = str(payload.get("tool", "tool"))
            self.snapshot.running_tool = tool
            self.snapshot.status = "tool"
            self._pending_tools[tool] = time.time()
            title = _tool_call_title(tool, payload.get("args"))
            return TranscriptBlock("tool", title, "", "running", turn=self.snapshot.turn, detail=True)
        if event_type == "tool_result":
            tool = str(payload.get("tool", "tool"))
            if self.snapshot.running_tool == tool:
                self.snapshot.running_tool = ""
            status = str(payload.get("status", "unknown"))
            if tool == "update_plan_state" and status == "success":
                self._update_plan_steps_from_metadata(payload.get("metadata"))
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
            parallel_summary = _parallel_result_summary(tool, payload.get("metadata"))
            if parallel_summary:
                parts.append(parallel_summary)
            if summary.output_chars > 0:
                parts.append(_format_size(summary.output_chars))
            if summary.return_code is not None:
                parts.append(f"rc={summary.return_code}")
            body = "  ".join(parts)
            if summary.error_summary:
                body += f"\n{summary.error_summary}"
            return TranscriptBlock("tool", tool, body, status, turn=self.snapshot.turn, detail=True)
        if event_type == "file_change":
            self.snapshot.dirty_count += 1
            return TranscriptBlock("file", "file changed", _payload_summary(payload), "changed", turn=self.snapshot.turn, detail=True)
        if event_type == "failure":
            self.snapshot.status = "needs attention"
            return TranscriptBlock("failure", "failure", _payload_summary(payload), "failed", turn=self.snapshot.turn, detail=False)
        if event_type == "agent_budget_warning":
            self.snapshot.status = "needs attention"
            return TranscriptBlock("status", "agent budget warning", _payload_summary(payload), "warning", turn=self.snapshot.turn, detail=False)
        if event_type == "agent_fallback":
            self.snapshot.status = "blocked"
            self.snapshot.running_tool = ""
            return TranscriptBlock("failure", "agent fallback", _payload_summary(payload), "blocked", turn=self.snapshot.turn, detail=False)
        if event_type == "approval_requested":
            self.pending_approval = payload
            return None
        if event_type == "approval_decided":
            self.pending_approval = None
            return None
        if event_type == "profile_switched":
            self.snapshot.profile = str(payload.get("profile") or self.snapshot.profile)
            self.snapshot.pending_plan = False
            previous = payload.get("previous_profile", "")
            current = payload.get("profile", "")
            reason = payload.get("reason", "")
            return TranscriptBlock("profile", "profile switched", f"{previous} -> {current} ({reason})")
        if event_type == "profile_route_decision":
            return TranscriptBlock(
                "profile",
                "profile route",
                _profile_route_summary(payload),
                "success" if not payload.get("fallback_used") else "fallback",
                turn=self.snapshot.turn,
                detail=True,
            )
        if event_type == "permission_mode_switched":
            self.snapshot.permission_mode = str(payload.get("permission_mode") or self.snapshot.permission_mode)
            return None
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
            phase = str(payload.get("phase") or "")
            labels = {
                "cleaning_older_outputs": "cleaning older outputs",
                "summarizing_history": "summarizing history",
                "auto_compaction_suspended": "auto-compaction suspended",
                "handoff_reset": "handoff reset",
            }
            label = labels.get(phase, "context compaction started")
            self.snapshot.status = label
            return TranscriptBlock("status", label, _payload_summary(payload), "running", turn=self.snapshot.turn, detail=True)
        if event_type == "context_compaction_committed":
            self.snapshot.status = "idle"
            tokens_saved = payload.get("tokens_saved", 0)
            body = f"tokens saved: {tokens_saved}" if tokens_saved else _payload_summary(payload)
            return TranscriptBlock("status", "context compacted", body, "success", turn=self.snapshot.turn, detail=True)
        if event_type == "context_anxiety_observed":
            self.snapshot.status = "context anxiety observed"
            return TranscriptBlock(
                "status",
                "context anxiety observed",
                _payload_summary(payload),
                "observed",
                turn=self.snapshot.turn,
                detail=True,
            )
        if event_type == "turn_summary":
            turn = _payload_turn(payload, self.snapshot.turn)
            self.snapshot.status = "idle"
            return None
        if event_type == "turn_finished":
            self.snapshot.status = "idle"
            self.snapshot.running_tool = ""
            self.snapshot.checkpoint = str(payload.get("checkpoint") or "")
            return None
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
            return TranscriptBlock("thought", "thinking", body, "thought", turn=self.snapshot.turn, detail=True)
        return None

    def add_block(self, block: TranscriptBlock | None) -> None:
        if block is not None:
            self.blocks.append(block)

    def _update_plan_steps_from_metadata(self, metadata: Any) -> None:
        if not isinstance(metadata, dict):
            return
        planning_state = metadata.get("planning_state")
        if not isinstance(planning_state, dict):
            return

        raw_steps = planning_state.get("steps")
        if not isinstance(raw_steps, list):
            return
        steps = [str(step).strip() for step in raw_steps if str(step).strip()]
        current_step = str(planning_state.get("current_step") or "").strip()
        raw_completed_steps = planning_state.get("completed_steps")
        if not isinstance(raw_completed_steps, list):
            raw_completed_steps = []
        completed_steps = {
            str(step).strip()
            for step in raw_completed_steps
            if str(step).strip()
        }

        plan_steps: list[PlanStep] = []
        for step in steps:
            if step in completed_steps:
                status = "completed"
            elif step == current_step:
                status = "current"
            else:
                status = "pending"
            plan_steps.append(PlanStep(step, status))
        self.plan_steps = plan_steps


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


def _payload_turn(payload: dict[str, Any], default: int) -> int:
    try:
        return int(payload.get("turn") or default)
    except (TypeError, ValueError):
        return default


def _approval_summary(payload: dict[str, Any]) -> str:
    parts = []
    for key in ("tool", "risk", "reason"):
        if key in payload:
            text = str(payload[key]).replace("\n", " ")
            if len(text) > 160:
                text = text[:157] + "..."
            parts.append(f"{key}={text}")
    return ", ".join(parts)


def _profile_route_summary(payload: dict[str, Any]) -> str:
    parts = []
    profile = payload.get("profile")
    if profile:
        parts.append(f"profile={profile}")
    confidence = payload.get("confidence")
    if confidence is not None:
        try:
            parts.append(f"confidence={float(confidence):.2f}")
        except (TypeError, ValueError):
            parts.append(f"confidence={confidence}")
    elapsed_ms = _coerce_float(payload.get("elapsed_ms"))
    if elapsed_ms is not None:
        parts.append(f"elapsed={_format_elapsed(elapsed_ms / 1000)}")
    if payload.get("fallback_used"):
        fallback_reason = str(payload.get("fallback_reason") or "fallback")
        parts.append(f"fallback={fallback_reason}")
    reason = str(payload.get("reason") or "").strip()
    if reason:
        parts.append(f"reason={reason}")
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


def _tool_call_title(tool: str, args: Any) -> str:
    if tool in {"parallel_commands", "parallel_agents"}:
        count = _parallel_tool_count_from_args(args)
        if count is not None:
            return f"{tool}({_format_tool_count(count)})"
    args_summary = _summarize_tool_args(args)
    return f"{tool}({args_summary})"


def _parallel_tool_count_from_args(args: Any) -> int | None:
    if not isinstance(args, dict):
        return None
    items = args.get("commands")
    if not isinstance(items, list):
        items = args.get("agents")
    if not isinstance(items, list):
        return None
    return len(items)


def _parallel_result_summary(tool: str, metadata: Any) -> str:
    if tool not in {"parallel_commands", "parallel_agents"} or not isinstance(metadata, dict):
        return ""
    items = metadata.get("items")
    count = _coerce_int(metadata.get("item_count"))
    success_count = _coerce_int(metadata.get("success_count"))
    failed_count = _coerce_int(metadata.get("failed_count"))
    if isinstance(items, list):
        count = count if count is not None else len(items)
        if success_count is None:
            success_count = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "success")
        if failed_count is None:
            failed_count = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "failed")
    if count is None:
        return ""
    parts = [_format_tool_count(count)]
    if success_count is not None:
        parts.append(f"success={success_count}")
    if failed_count is not None:
        parts.append(f"failed={failed_count}")
    return " ".join(parts)


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_tool_count(count: int) -> str:
    return f"同时执行了{count}个工具"


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
