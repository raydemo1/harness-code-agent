"""Task tracking middleware."""
from __future__ import annotations

import logging
import re

from ...agent.acceptance import AcceptanceError
from ..shell_classification import classify_safe_shell_command
from .base import AgentMiddleware, MAIN_AGENT_NAMES


log = logging.getLogger("harness")


class TaskTrackingEnforcementMiddleware(AgentMiddleware):
    """Hard-require planning updates for tracked mode."""

    ACTION_TOOLS = {"run_bash", "write_file", "apply_patch", "delegate_agent", "browser_test"}

    def __init__(
        self,
        *,
        enforce_acceptance: bool = False,
        require_start_after_n_actions: int | None = None,
    ):
        self.enforce_acceptance = bool(enforce_acceptance)
        self.require_start_after_n_actions = require_start_after_n_actions
        self._missing_start_reminded = False

    def before_tool(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or runtime_state is None:
            return None
        if tool_name == "update_plan_state":
            if not self.enforce_acceptance:
                return None
            update_kind = str(tool_args.get("update_kind") or "").strip().lower()
            if update_kind == "start" and not tool_args.get("acceptance_checks"):
                return (
                    "[blocked] Tracked planning start requires 1-10 acceptance_checks "
                    "with text, source, and verification_command."
                )
            if update_kind == "final":
                return self._validate_acceptance_final(tool_args, runtime_state)
            return None
        if tool_name not in self.ACTION_TOOLS:
            return None
        board = runtime_state.task_board
        if _is_pre_start_read_only_delegate(tool_name, tool_args, board):
            return None
        if self._requires_tracked_start(runtime_state):
            return (
                "[blocked] This task has reached multi-step execution. "
                "Call update_plan_state with mode=\"tracked\", update_kind=\"start\", "
                "and concrete acceptance_checks before more edits or commands."
            )
        if board.planning_mode in {"unset", "skip"}:
            return None
        if board.replan_required:
            reason = f" Reason: {board.replan_reason}" if board.replan_reason else ""
            return (
                "[blocked] Replan is required before more tracked actions. "
                "Call update_plan_state with update_kind=\"replan\"." + reason
            )
        if board.requires_update:
            return (
                "[blocked] Update planning state before more edits or commands. "
                "Call update_plan_state with update_kind=\"replan\" or update_kind=\"progress\"."
            )
        return None

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or runtime_state is None:
            return None
        if result.startswith("[error]") or result.startswith("[blocked]"):
            return None

        board = runtime_state.task_board
        if tool_name == "update_plan_state":
            board.requires_update = False
            if board.result_status:
                board.needs_final_update = False
            board.actions_since_progress = 0
            return None

        if tool_name in self.ACTION_TOOLS:
            if _is_pre_start_read_only_delegate(tool_name, tool_args, board):
                return None
            if (
                board.planning_mode == "tracked"
                and board.update_count == 0
                and tool_name == "run_bash"
                and _is_read_only_probe(tool_args.get("command", ""))
            ):
                return None
            runtime_state.action_tool_count += 1
            board.action_count = runtime_state.action_tool_count
            if board.planning_mode == "tracked":
                board.needs_final_update = True
                if (
                    board.update_count == 0
                    and runtime_state.action_tool_count >= 5
                    and not self._missing_start_reminded
                ):
                    self._missing_start_reminded = True
                    return (
                        "[SYSTEM] You have completed several exploratory actions. "
                        "Now is a good time to formalize the plan: call update_plan_state with "
                        "update_kind=\"start\" and concrete acceptance_checks before diving "
                        "deeper into implementation."
                    )
        return None

    def _requires_tracked_start(self, runtime_state) -> bool:
        if not self.enforce_acceptance or self.require_start_after_n_actions is None:
            return False
        try:
            threshold = int(self.require_start_after_n_actions)
        except (TypeError, ValueError):
            return False
        if threshold <= 0:
            return False
        board = runtime_state.task_board
        if board.update_count > 0:
            return False
        if board.planning_mode not in {"unset", "skip", "tracked"}:
            return False
        return int(getattr(runtime_state, "action_tool_count", 0) or 0) >= threshold

    def _validate_acceptance_final(self, tool_args: dict, runtime_state) -> str | None:
        result_status = str(tool_args.get("result_status") or "").strip().lower()
        if result_status != "success":
            return None
        acceptance = runtime_state.task_board.acceptance
        acceptance_snapshot = acceptance.snapshot()
        if not acceptance_snapshot["checks"]:
            return "[blocked] success requires at least one active acceptance check."
        weak_command = _weak_acceptance_command(acceptance_snapshot["checks"])
        if weak_command:
            return (
                "[blocked] success requires each acceptance check to have a real verification command. "
                f"Replace weak verification_command for {weak_command['id']!s}: "
                f"{weak_command['verification_command']!r}."
            )
        checkpoint = acceptance.checkpoint()
        try:
            acceptance.validate_final(
                result_status=result_status,
                expected_revision=tool_args.get("acceptance_revision"),
                raw_results=tool_args.get("check_results"),
            )
        except AcceptanceError as exc:
            return f"[blocked] {exc}"
        finally:
            acceptance.rollback(checkpoint, if_revision=acceptance.revision)
        facts = runtime_state.execution_facts
        if facts.last_foreground_shell_sequence == 0:
            return (
                "[blocked] success requires at least one foreground run_bash command "
                "with exit_code == 0."
            )
        if facts.last_business_edit_sequence >= facts.last_foreground_shell_sequence:
            return (
                "[blocked] success requires a foreground run_bash with exit_code == 0 "
                "after the last business file edit."
            )
        if not facts.last_foreground_shell_success:
            return (
                "[blocked] success requires the last foreground run_bash command "
                "to finish with exit_code == 0."
            )
        return None

    def pre_exit(self, messages: list[dict], runtime_state=None,
                 agent_name: str | None = None) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or runtime_state is None:
            return None
        board = runtime_state.task_board
        if board.planning_mode == "tracked" and board.needs_final_update:
            return (
                "[SYSTEM] Before finishing, call update_plan_state with update_kind=\"final\". "
                "Include result_status, validation, and remaining_issues."
            )
        return None


_WEAK_VERIFICATION_PATTERNS = (
    r"^\s*manual\s*$",
    r"(?:^|[;&|]\s*)echo\s+['\"]?(?:checked|verified|ok|done)\b",
    r"(?:^|[;&|]\s*)echo\s+['\"]?(?:check(?:ed)?\s+manually|manual(?:ly)?)\b",
    r"\bcheck(?:ed)?\s+manually\b",
    r">\s*/dev/null(?:\s+2>&1)?\s*[;&|]+\s*echo\b",
    r"\bchecked by design\b",
    r"\bverified by design\b",
    r"\bby inspection\b",
    r"\bno command\b",
    r"\bnot applicable\b",
)


def _weak_acceptance_command(checks: list[dict]) -> dict | None:
    for check in checks:
        command = str(check.get("verification_command") or "").strip()
        if not command:
            return check
        if any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in _WEAK_VERIFICATION_PATTERNS):
            return check
    return None


def _is_read_only_probe(command: str) -> bool:
    return classify_safe_shell_command(command) == "read"


def _is_pre_start_read_only_delegate(tool_name: str, tool_args: dict, board) -> bool:
    return (
        tool_name == "delegate_agent"
        and str((tool_args or {}).get("agent_profile") or "").strip().lower().replace("-", "_") != "patch"
        and board.update_count == 0
        and board.planning_mode in {"unset", "skip", "tracked"}
    )
