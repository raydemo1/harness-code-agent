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
    running_tool: str = ""
    status: str = "idle"
    dirty_count: int = 0
    context_tokens: int = 0
    context_window_tokens: int = 0


@dataclass
class TranscriptBlock:
    kind: str
    title: str
    body: str = ""
    status: str = ""
    turn: int | None = None


@dataclass(frozen=True)
class PlanStep:
    text: str
    status: str


@dataclass
class TuiState:
    snapshot: SessionStatusSnapshot
    blocks: list[TranscriptBlock] = field(default_factory=list)
    plan_steps: list[PlanStep] = field(default_factory=list)
    _pending_tools: dict[str, float] = field(default_factory=dict)

    def apply_event(self, event: Any) -> TranscriptBlock | None:
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_type = str(data.get("type", ""))
        payload = data.get("payload") or {}

        if event_type == "session_started":
            self.snapshot.status = "ready"
            self.snapshot.profile = str(payload.get("profile") or self.snapshot.profile)
            if payload.get("session_id"):
                self.snapshot.session_id = str(payload.get("session_id"))
            return None
        if event_type == "user_input":
            self.snapshot.turn = int(payload.get("turn") or self.snapshot.turn)
            return TranscriptBlock("user", f"第 {self.snapshot.turn} 回合", str(payload.get("text", "")), turn=self.snapshot.turn)
        if event_type == "turn_started":
            self.snapshot.status = "running"
            self.snapshot.turn = int(payload.get("turn") or self.snapshot.turn)
            return None
        if event_type == "assistant_message":
            self.snapshot.status = "idle"
            turn = _payload_turn(payload, self.snapshot.turn)
            return TranscriptBlock("assistant", "助手", str(payload.get("text", "")), turn=turn)
        if event_type == "tool_call":
            return self._apply_tool_call(payload)
        if event_type == "tool_result":
            return self._apply_tool_result(payload)
        if event_type == "middleware_activity":
            return self._apply_middleware_activity(payload)
        if event_type == "file_change":
            self.snapshot.dirty_count += 1
            operation = str(payload.get("operation") or "changed")
            path = str(payload.get("path") or "")
            verb = _FILE_OPERATION_LABELS.get(operation, operation)
            diff = str(payload.get("diff") or "")
            return TranscriptBlock("file", f"{verb} {path}", diff, "changed", turn=self.snapshot.turn)
        if event_type == "failure":
            self.snapshot.status = "needs attention"
            return self._apply_failure(payload)
        if event_type == "agent_budget_warning":
            return None
        if event_type == "agent_fallback":
            self.snapshot.status = "blocked"
            self.snapshot.running_tool = ""
            reason = str(payload.get("reason") or "stopped").replace("_", " ")
            body = f"已停止：{_FAILURE_LABELS.get(reason, reason)}"
            last_tool = str(payload.get("last_tool") or "")
            if last_tool:
                body += f"（最后工具：{last_tool}）"
            return TranscriptBlock("failure", "代理已停止", body, "blocked", turn=self.snapshot.turn)
        if event_type == "approval_requested":
            return None
        if event_type == "approval_decided":
            return None
        if event_type == "profile_switched":
            self.snapshot.profile = str(payload.get("profile") or self.snapshot.profile)
            self.snapshot.pending_plan = False
            previous = payload.get("previous_profile", "")
            current = payload.get("profile", "")
            reason = payload.get("reason", "")
            body = f"{previous} -> {current}"
            if reason:
                body += f" ({reason})"
            return TranscriptBlock("profile", "配置已切换", body)
        if event_type == "profile_route_decision":
            profile = str(payload.get("profile") or "")
            fallback_used = bool(payload.get("fallback_used"))
            reason = str(payload.get("fallback_reason") or "").strip()
            body = f"已路由到 {profile}" if profile else "已路由"
            if fallback_used:
                body += f"（兜底：{reason or '未知'}）"
            return TranscriptBlock("profile", "配置路由", body, "fallback" if fallback_used else "", turn=self.snapshot.turn)
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
                "计划已准备",
                f"{path}{suffix}\n[计划已准备]  [如需修改，请描述要调整的内容]",
                "pending",
            )
        if event_type == "context_compaction_started":
            self.snapshot.status = "compacting context"
            return None
        if event_type == "context_compaction_committed":
            self.snapshot.status = "idle"
            tokens_saved = payload.get("tokens_saved", 0)
            body = f"已节省约 {tokens_saved} 个 token" if tokens_saved else "上下文已压缩"
            return TranscriptBlock("status", "上下文已压缩", body, "success", turn=self.snapshot.turn)
        if event_type == "context_anxiety_observed":
            return None
        if event_type == "turn_summary":
            self.snapshot.status = "idle"
            return None
        if event_type == "turn_finished":
            self.snapshot.status = "idle"
            self.snapshot.running_tool = ""
            return None
        if event_type == "session_finished":
            self.snapshot.status = str(payload.get("status") or "closed")
            return None
        if event_type == "thought_started":
            self.snapshot.status = "thinking"
            return None
        if event_type == "thought_finished":
            duration = payload.get("duration_seconds", 0)
            truncated = payload.get("truncated", False)
            body = f"for {_format_elapsed(duration)}"
            if truncated:
                body += " (truncated)"
            return TranscriptBlock("thought", "思考", body, "thought", turn=self.snapshot.turn)
        return None

    def add_block(self, block: TranscriptBlock | None) -> None:
        if block is not None:
            self.blocks.append(block)

    def _apply_tool_call(self, payload: dict[str, Any]) -> TranscriptBlock | None:
        tool = str(payload.get("tool", "tool"))
        self.snapshot.running_tool = tool
        self.snapshot.status = "tool"
        self._pending_tools[tool] = time.time()
        title = _tool_call_title(tool, payload.get("args"))
        return TranscriptBlock("tool", title, "", "running", turn=self.snapshot.turn)

    def _apply_tool_result(self, payload: dict[str, Any]) -> TranscriptBlock | None:
        tool = str(payload.get("tool", "tool"))
        if self.snapshot.running_tool == tool:
            self.snapshot.running_tool = ""
        status = str(payload.get("status", "unknown"))
        if tool == "update_plan_state" and status == "success":
            self._update_plan_steps_from_metadata(payload.get("metadata"))
            self.snapshot.status = "running"
            return TranscriptBlock(
                "plan",
                "计划",
                _format_plan_steps(self.plan_steps),
                "updated",
                turn=self.snapshot.turn,
            )
        self.snapshot.status = "running"
        start_time = self._pending_tools.pop(tool, None)
        elapsed = time.time() - start_time if start_time else 0.0
        output = str(payload.get("output") or "")
        error = str(payload.get("error") or "")
        return_code = payload.get("return_code")
        parts = []
        if elapsed > 0:
            parts.append(_format_elapsed(elapsed))
        parallel_summary = _parallel_result_summary(tool, payload.get("metadata"))
        if parallel_summary:
            parts.append(parallel_summary)
        if output:
            parts.append(_format_size(len(output)))
        if return_code is not None and status != "success":
            parts.append(f"退出码 {return_code}")
        body = "  ".join(parts)
        if error:
            body += f"\n{_tail(error, 120)}" if body else _tail(error, 120)
        return TranscriptBlock("tool", tool, body, status, turn=self.snapshot.turn)

    def _apply_middleware_activity(self, payload: dict[str, Any]) -> TranscriptBlock:
        tool = str(payload.get("tool") or "tool")
        outcome = str(payload.get("outcome") or "passed")
        hooks = _coerce_int(payload.get("hooks")) or 0
        duration_ms = float(payload.get("duration_ms") or 0.0)
        parts = [tool]
        if hooks:
            parts.append(f"{hooks} 个钩子")
        parts.append("<0.1 毫秒" if duration_ms < 0.1 else f"{duration_ms:.1f} 毫秒")
        sources = payload.get("sources")
        if isinstance(sources, list) and sources:
            labels = [_middleware_source_label(str(source)) for source in sources]
            parts.append(", ".join(labels))
        return TranscriptBlock(
            "middleware",
            "策略管线",
            "  ·  ".join(parts),
            outcome,
            turn=self.snapshot.turn,
        )

    def _apply_failure(self, payload: dict[str, Any]) -> TranscriptBlock | None:
        category = str(payload.get("category") or "error").replace("_", " ")
        message = str(payload.get("message") or "")
        tool = str(payload.get("tool") or "")
        body = f"{_FAILURE_LABELS.get(category, category)}：{message}" if message else _FAILURE_LABELS.get(category, category)
        if tool:
            body += f"\n执行工具：{tool}"
        return TranscriptBlock("failure", "错误", body, "failed", turn=self.snapshot.turn)

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


_FILE_OPERATION_LABELS = {
    "write": "已写入",
    "write_file": "已写入",
    "edit": "已编辑",
    "apply_patch": "已编辑",
    "delete": "已删除",
    "create": "已创建",
    "rename": "已重命名",
    "changed": "已变更",
}

_FAILURE_LABELS = {
    "stopped": "已停止",
    "time budget exhausted": "超出时间预算",
    "runtime error": "运行时错误",
    "api error": "模型接口错误",
    "error": "错误",
}

# Tools whose call titles are reduced to their single most useful argument.
_TOOL_PRIMARY_ARGS = {
    "run_bash": "command",
    "read_file": "path",
    "write_file": "path",
    "apply_patch": "path",
    "edit_file": "path",
    "list_files": "directory",
    "search_files": "query",
    "delegate_agent": "task",
    "browser_test": "url",
    "web_fetch": "url",
}


def _format_plan_steps(steps: list[PlanStep]) -> str:
    markers = {
        "completed": "✓",
        "current": "›",
        "pending": "○",
    }
    return "\n".join(f"{markers.get(step.status, '○')} {step.text}" for step in steps)


def _payload_turn(payload: dict[str, Any], default: int) -> int:
    try:
        return int(payload.get("turn") or default)
    except (TypeError, ValueError):
        return default


def _summarize_tool_args(args: Any) -> str:
    if not isinstance(args, dict):
        return str(args or "")
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", " ")
        if key == "content":
            text = f"[{len(text)} chars]"
        elif len(text) > 120:
            text = text[:117] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def _tool_call_title(tool: str, args: Any) -> str:
    if tool in {"parallel_commands", "parallel_agents"}:
        count = _parallel_tool_count_from_args(args)
        if count is not None:
            return f"{tool}({_format_tool_count(count)})"
    primary_key = _TOOL_PRIMARY_ARGS.get(tool)
    if primary_key and isinstance(args, dict) and args.get(primary_key):
        value = str(args[primary_key]).replace("\n", " ")
        if len(value) > 160:
            value = value[:157] + "..."
        return f"{tool}({value})"
    return f"{tool}({_summarize_tool_args(args)})"


def _middleware_source_label(source: str) -> str:
    label = source.removesuffix("Middleware")
    aliases = {
        "Permission": "权限",
        "ToolPolicy": "工具策略",
        "RecoveryStrategy": "恢复",
        "LoopDetection": "循环保护",
        "TaskTrackingEnforcement": "任务跟踪",
        "AcceptanceReview": "验收审查",
        "StaticVerifier": "验证",
    }
    return aliases.get(label, label)


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
        parts.append(f"{success_count} 成功")
    if failed_count is not None:
        parts.append(f"{failed_count} 失败")
    return " ".join(parts)


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_tool_count(count: int) -> str:
    return f"{count} 个工具"


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
