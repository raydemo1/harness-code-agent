"""Task tracking middleware."""
from __future__ import annotations

import logging

from .base import AgentMiddleware, MAIN_AGENT_NAMES


log = logging.getLogger("harness")


class TaskTrackingMiddleware(AgentMiddleware):
    """
    Encourages the agent to maintain explicit task tracking for multi-step work.

    After the agent has made several tool calls without writing any tracking
    artifact, injects a reminder to decompose and track progress.

    Inspired by ForgeCode's todo_write enforcement, which was their single
    biggest improvement (38% → 66% on TB2).

    This is a softer version — it nudges rather than hard-blocks, since
    not all tasks need decomposition. But for complex multi-step tasks,
    the nudge is enough to trigger the behavior.
    """

    def __init__(self, nudge_after_n_tools: int = 8):
        self.nudge_after_n_tools = nudge_after_n_tools
        self.tool_call_count = 0
        self._nudged = False

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        self.tool_call_count += 1

        if self._nudged or self.tool_call_count < self.nudge_after_n_tools:
            return None

        # Check if agent has already written any tracking/progress notes
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and "progress" in content.lower():
                # Agent seems to be tracking already
                return None
            # Check if agent wrote to a tracking file
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    if fn.get("name") == "write_file":
                        args_str = fn.get("arguments", "")
                        if any(kw in args_str.lower() for kw in ["todo", "progress", "checklist", "tracker"]):
                            return None

        self._nudged = True
        log.info("Task tracking: nudging agent to track progress")
        return (
            "[SYSTEM] You have made several tool calls. For complex tasks, "
            "tracking your progress helps avoid skipping steps or repeating work.\n"
            "Consider: What steps remain? What have you completed? What still needs verification?\n"
            "Keep a mental checklist and verify each requirement before finishing."
        )


class TaskTrackingEnforcementMiddleware(AgentMiddleware):
    """Hard-require planning updates for light/full planning modes."""

    ACTION_TOOLS = {"run_bash", "write_file", "apply_patch", "consult_subagent", "browser_test"}

    def __init__(self, *, enforce_acceptance: bool = False):
        self.enforce_acceptance = bool(enforce_acceptance)

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
                    "[blocked] Terminal planning start requires 1-10 acceptance_checks "
                    "with text, source, and verification_command."
                )
            if update_kind == "final":
                return self._validate_terminal_final(tool_args, runtime_state)
            return None
        if tool_name not in self.ACTION_TOOLS:
            return None
        board = runtime_state.task_board
        if board.planning_mode in {"unset", "skip"}:
            return None
        if board.planning_mode in {"light", "full"} and board.update_count == 0:
            return (
                "[blocked] Planning mode is light/full but start state is missing. "
                "Call update_plan_state with update_kind=\"start\" before tracked action tools."
            )
        if board.requires_approval:
            return (
                "[blocked] The current full plan requires approval before more tracked actions. "
                "Wait for user confirmation, then call update_plan_state with requires_approval=false before continuing."
            )
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
            runtime_state.action_tool_count += 1
            board.action_count = runtime_state.action_tool_count
            if board.planning_mode in {"light", "full"}:
                board.needs_final_update = True
        return None

    def _validate_terminal_final(self, tool_args: dict, runtime_state) -> str | None:
        result_status = str(tool_args.get("result_status") or "").strip().lower()
        if result_status != "success":
            return None
        acceptance = runtime_state.task_board.acceptance
        if not acceptance.snapshot()["checks"]:
            return "[blocked] success requires at least one active acceptance check."
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
        if board.planning_mode in {"light", "full"} and board.needs_final_update:
            return (
                "[SYSTEM] Before finishing, call update_plan_state with update_kind=\"final\". "
                "Include result_status, validation, and remaining_issues."
            )
        return None
