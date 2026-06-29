"""Planning-state tool implementation."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from ... import config
from ...agent.acceptance import AcceptanceError
from ..tool_context import ToolContext
from ..tool_result import ToolResult


def update_plan_state(
    mode: str,
    update_kind: str,
    goal: str,
    steps: list[str],
    current_step: str,
    completed_steps: list[str],
    blockers: list[str],
    next_action: str,
    plan_markdown: str | None = None,
    replan_reason: str | None = None,
    requires_approval: bool = False,
    result_status: str | None = None,
    validation: str | None = None,
    remaining_issues: list[str] | None = None,
    acceptance_checks: list[dict] | None = None,
    acceptance_revision: int | None = None,
    acceptance_operations: list[dict] | None = None,
    check_results: list[dict] | None = None,
    runtime_state=None,
    agent_name: str | None = None,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    """Update tracked todo and acceptance state."""
    if runtime_state is None:
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] update_plan_state requires runtime state",
            error="update_plan_state requires runtime state",
            metadata={"status_source": "runtime"},
        )

    mode = (mode or "").strip().lower()
    update_kind = (update_kind or "").strip().lower()
    goal = (goal or "").strip()
    current_step = (current_step or "").strip()
    next_action = (next_action or "").strip()
    plan_markdown = (plan_markdown or "").strip()
    replan_reason = (replan_reason or "").strip()
    result_status = (result_status or "").strip()
    validation = (validation or "").strip()
    remaining_issues_provided = remaining_issues is not None
    steps = [str(step).strip() for step in (steps or []) if str(step).strip()]
    completed_steps = [str(step).strip() for step in (completed_steps or []) if str(step).strip()]
    blockers = [str(item).strip() for item in (blockers or []) if str(item).strip()]
    remaining_issues = [str(item).strip() for item in (remaining_issues or []) if str(item).strip()]
    board = runtime_state.task_board
    normalized_step_notes: list[str] = []

    if board.replan_required:
        if update_kind == "start":
            update_kind = "replan"
        if update_kind == "replan" and not replan_reason:
            replan_reason = (
                board.replan_reason
                or getattr(runtime_state.recovery, "failure_signature", "")
                or "Recovery strategy requires a new plan."
            )

    if mode != "tracked":
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] mode must be: tracked",
            error="mode must be: tracked",
            metadata={"status_source": "validation"},
        )
    if update_kind not in {"start", "progress", "replan", "final"}:
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] update_kind must be one of: start, progress, replan, final",
            error="update_kind must be one of: start, progress, replan, final",
            metadata={"status_source": "validation"},
        )
    if board.replan_required and update_kind != "replan":
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] a required replan can only be cleared by update_kind=replan",
            error="a required replan can only be cleared by update_kind=replan",
            metadata={"status_source": "validation"},
        )
    if update_kind == "replan":
        missing_steps = [
            item
            for item in [*completed_steps, current_step]
            if item and item not in steps
        ]
        if missing_steps:
            normalized_step_notes = list(dict.fromkeys(missing_steps))
            steps = [*normalized_step_notes, *steps]
    if not goal:
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] update_plan_state requires a non-empty goal",
            error="update_plan_state requires a non-empty goal",
            metadata={"status_source": "validation"},
        )
    if not steps:
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] update_plan_state requires at least one step",
            error="update_plan_state requires at least one step",
            metadata={"status_source": "validation"},
        )
    if update_kind != "final" and current_step not in steps:
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] current_step must be one of the declared steps",
            error="current_step must be one of the declared steps",
            metadata={"status_source": "validation"},
        )
    if any(step not in steps for step in completed_steps):
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] completed_steps must be a subset of steps",
            error="completed_steps must be a subset of steps",
            metadata={"status_source": "validation"},
        )
    if update_kind != "final" and not next_action:
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] update_plan_state requires a non-empty next_action",
            error="update_plan_state requires a non-empty next_action",
            metadata={"status_source": "validation"},
        )
    if update_kind == "final" and (not result_status or not validation or not remaining_issues_provided):
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] final update requires result_status, validation, and remaining_issues",
            error="final update requires result_status, validation, and remaining_issues",
            metadata={"status_source": "validation"},
        )
    if update_kind == "replan" and not replan_reason:
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output="[error] replan update requires replan_reason",
            error="replan update requires replan_reason",
            metadata={"status_source": "validation"},
        )
    acceptance_checkpoint = board.acceptance.checkpoint()
    acceptance_revision_before_update = acceptance_checkpoint["revision"]
    acceptance_revision_after_update = acceptance_revision_before_update
    reused_existing_acceptance = False
    acceptance_replaced = False
    try:
        if update_kind == "start" and acceptance_checks is not None:
            has_existing = bool(
                board.acceptance.revision
                or board.acceptance.checks
                or board.acceptance.removed_checks
            )
            if has_existing:
                existing_keys = {
                    (
                        c.get("text", ""),
                        c.get("source", ""),
                        c.get("verification_command", ""),
                    )
                    for c in board.acceptance.checks
                }
                new_keys = {
                    (
                        str(check.get("text", "")),
                        str(check.get("source", "")),
                        str(check.get("verification_command", "")),
                    )
                    for check in acceptance_checks
                }
                if existing_keys == new_keys:
                    reused_existing_acceptance = True
                else:
                    replace_ops: list[dict] = [
                        {"operation": "remove", "id": c["id"], "reason": "acceptance replacement on re-start"}
                        for c in board.acceptance.checks
                    ]
                    for check in acceptance_checks:
                        replace_ops.append({
                            "operation": "add",
                            "text": check.get("text"),
                            "source": check.get("source"),
                            "verification_command": check.get("verification_command"),
                            "reason": "acceptance replacement on re-start",
                        })
                    board.acceptance.apply_operations(
                        replace_ops,
                        expected_revision=board.acceptance.revision,
                    )
                    acceptance_replaced = True
            else:
                board.acceptance.initialize(acceptance_checks)
        elif acceptance_operations:
            board.acceptance.apply_operations(
                acceptance_operations,
                expected_revision=acceptance_revision,
            )
        acceptance_revision_after_update = board.acceptance.revision
        if update_kind == "final" and board.acceptance.revision:
            board.acceptance.validate_final(
                result_status=result_status,
                expected_revision=(
                    acceptance_revision_after_update
                    if acceptance_operations
                    else acceptance_revision
                ),
                raw_results=check_results,
            )
    except (AcceptanceError, TypeError, ValueError) as exc:
        board.acceptance.rollback(
            acceptance_checkpoint,
            if_revision=acceptance_revision_after_update,
        )
        acceptance_snapshot = board.acceptance.snapshot()
        current_acceptance = {
            "revision": acceptance_snapshot["revision"],
            "checks": acceptance_snapshot["checks"],
        }
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output=(
                f"[error] {exc}\nCurrent acceptance state: "
                + json.dumps(current_acceptance, ensure_ascii=False, sort_keys=True)
                + _acceptance_recovery_hint(str(exc), acceptance_snapshot)
            ),
            error=str(exc),
            metadata={
                "status_source": "validation",
                "acceptance": board.acceptance.snapshot(),
            },
        )

    workspace = tool_context.workspace.root if tool_context is not None else Path(config.WORKSPACE)
    previous_revision = int(getattr(board, "plan_revision", 0) or 0)
    plan_revision = previous_revision
    changed_files = _planning_changed_files(runtime_state, tool_context)

    payload = {
        "mode": mode,
        "update_kind": update_kind,
        "goal": goal,
        "steps": steps,
        "current_step": current_step,
        "completed_steps": completed_steps,
        "blockers": blockers,
        "next_action": next_action,
        "update_count": board.update_count + 1,
        "action_count": int(getattr(board, "action_count", runtime_state.action_tool_count) or 0),
        "changed_file_count": len(changed_files),
        "requires_approval": False,
        "requires_update": False,
        "needs_final_update": update_kind != "final" and bool(getattr(board, "needs_final_update", False)),
        "replan_required": False,
        "replan_reason": replan_reason,
        "plan_revision": plan_revision,
        "result_status": result_status,
        "validation": validation,
        "remaining_issues": remaining_issues,
        "acceptance": board.acceptance.snapshot(),
        "updated_at": _utc_timestamp(),
    }
    state_path = _planning_state_path(workspace, runtime_state, tool_context)
    ok, error = _atomic_write_json(state_path, payload)
    if not ok:
        board.acceptance.rollback(
            acceptance_checkpoint,
            if_revision=acceptance_revision_after_update,
        )
        return ToolResult(
            tool="update_plan_state",
            status="failed",
            output=f"[error] Failed to write state.json atomically: {error}",
            error=f"Failed to write state.json atomically: {error}",
            metadata={"status_source": "native"},
        )

    written = [str(state_path.relative_to(workspace))]

    board.goal = goal
    board.steps = steps
    board.current_step = current_step
    board.completed_steps = completed_steps
    board.blockers = blockers
    board.next_action = next_action
    board.update_count = payload["update_count"]
    board.action_count = payload["action_count"]
    board.changed_files = changed_files
    board.requires_approval = False
    board.requires_update = False
    board.needs_final_update = False if update_kind == "final" else bool(getattr(board, "needs_final_update", False))
    board.replan_required = False
    board.replan_reason = replan_reason
    board.plan_revision = plan_revision
    board.result_status = result_status
    board.validation = validation
    board.remaining_issues = remaining_issues
    board.actions_since_progress = 0
    board.planning_mode = mode
    if update_kind == "replan":
        runtime_state.recovery.replan_attempt_count += 1
        runtime_state.recovery.mode = "PROBE"
        runtime_state.recovery.probe_in_flight = False

    plan_update_count = payload["update_count"]
    revision_status = ""
    if board.acceptance.revision:
        revision_word = (
            "changed"
            if acceptance_revision_after_update != acceptance_revision_before_update
            else "unchanged"
        )
        revision_status = (
            f"\nAcceptance revision {revision_word}: "
            f"{acceptance_revision_before_update} -> {acceptance_revision_after_update}. "
            f"Use acceptance_revision={acceptance_revision_after_update} for future "
            "acceptance_operations or final check_results."
        )
        if reused_existing_acceptance:
            revision_status += (
                "\nAcceptance checks were already initialized; kept the existing "
                f"revision {acceptance_revision_after_update}."
            )
        if acceptance_replaced:
            revision_status += (
                "\nAcceptance checks replaced on re-start: removed previous checks "
                f"and added {len(acceptance_checks)} new check(s). "
                f"Revision bumped to {acceptance_revision_after_update}."
            )
    if normalized_step_notes:
        revision_status += (
            "\nReplan steps normalized to retain completed/current work: "
            + ", ".join(normalized_step_notes)
            + "."
        )
    status_output = f"\nPlan update count: {plan_update_count}." + revision_status
    acceptance_output = ""
    if board.acceptance.revision:
        acceptance_snapshot = board.acceptance.snapshot()
        acceptance_view = {
            "revision": acceptance_snapshot["revision"],
            "checks": acceptance_snapshot["checks"],
            "review_status": acceptance_snapshot["review_status"],
            "review_warning": acceptance_snapshot["review_warning"],
            "review_truncated": acceptance_snapshot["review_truncated"],
        }
        if update_kind == "final":
            acceptance_view["check_results"] = acceptance_snapshot["check_results"]
        acceptance_output = (
            "\nAcceptance state: "
            + json.dumps(acceptance_view, ensure_ascii=False, sort_keys=True)
        )
    return ToolResult(
        tool="update_plan_state",
        status="success",
        output="Updated plan state: " + ", ".join(written) + status_output + acceptance_output,
        metadata={
            "status_source": "native",
            "planning_state": payload,
            "file_changes": [
                {"path": path, "operation": "write_file", "snapshot_path": None}
                for path in written
            ],
        },
    )


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _acceptance_recovery_hint(error: str, acceptance_snapshot: dict) -> str:
    if "stale acceptance_revision" not in str(error):
        return ""
    revision = acceptance_snapshot.get("revision")
    return (
        f"\nRetry with acceptance_revision={revision}. If you still want to modify "
        "acceptance checks, include the same corrected acceptance_operations again; "
        "a failed update did not apply them."
    )


def _planning_state_path(workspace: Path, runtime_state, tool_context: ToolContext | None) -> Path:
    session_id = (
        (tool_context.session_id if tool_context is not None else None)
        or getattr(runtime_state, "session_id", None)
        or "default"
    )
    safe_session_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(session_id))
    return workspace / ".harness" / "sessions" / safe_session_id / "planning" / "state.json"


def _planning_changed_files(runtime_state, tool_context: ToolContext | None) -> list[str]:
    if tool_context is not None:
        return [str(path) for path in getattr(tool_context.workspace, "changed_files", [])]
    board = runtime_state.task_board
    return [str(path) for path in getattr(board, "changed_files", [])]


def _atomic_write_json(path: Path, payload: dict) -> tuple[bool, str | None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    json.loads(text)
    temp_path = path.parent / f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(temp_path.read_text(encoding="utf-8"))
        os.replace(temp_path, path)
        return True, None
    except Exception as exc:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        return False, str(exc)
