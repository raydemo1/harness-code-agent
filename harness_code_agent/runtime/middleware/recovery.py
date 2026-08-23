"""Recovery strategy middleware."""
from __future__ import annotations

from typing import ClassVar

from ..permissions import is_read_only_command
from ..shell_classification import classify_safe_shell_command
from ..tool_result import ToolResult
from .base import (
    MAIN_AGENT_NAMES,
    AgentMiddleware,
    result_text,
    tool_blocked,
    tool_failed,
)


class RecoveryStrategyMiddleware(AgentMiddleware):
    """Classify repeated failures and constrain the next class of actions."""

    ENV_ERROR_PATTERNS = (
        "command not found",
        "permission denied",
        "no such file or directory",
        "externally-managed-environment",
        "no module named",
        "modulenotfounderror",
    )
    ACTION_TOOLS: ClassVar[set] = {"run_bash", "write_file", "apply_patch", "delegate_agent", "browser_test"}
    VERIFICATION_FAILURE_PATTERNS = (
        "assert",
        "failed",
        "failure",
        "mismatch",
        "expected",
        "traceback",
    )

    def __init__(self):
        self._edit_attempts: dict[str, int] = {}

    def _set_mode(self, runtime_state, mode: str) -> None:
        if runtime_state.task_board.planning_mode == "todo":
            # Lightweight todo mode must not silently grow into a hard recovery
            # state machine. The failed result is already visible to the model
            # and the TUI; it can revise the checklist or escalate explicitly.
            return
        runtime_state.recovery.mode = mode
        runtime_state.task_board.requires_update = True
        if mode in {"SPEC_RECHECK", "RETHINK"}:
            runtime_state.task_board.replan_required = True
            runtime_state.task_board.replan_reason = (
                runtime_state.recovery.failure_signature
                or "Recovery strategy requires a new plan."
            )

    def _clear_mode(self, runtime_state) -> None:
        runtime_state.recovery.mode = "NORMAL"
        runtime_state.recovery.failure_signature = ""
        runtime_state.recovery.repeat_count = 0
        runtime_state.recovery.replan_attempt_count = 0
        runtime_state.recovery.probe_in_flight = False

    def _register_failure(self, signature: str, runtime_state) -> None:
        recovery = runtime_state.recovery
        signature = signature.strip()
        if not signature:
            return
        if recovery.failure_signature == signature:
            recovery.repeat_count += 1
        else:
            recovery.failure_signature = signature
            recovery.repeat_count = 1

    def _is_env_failure(self, text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in self.ENV_ERROR_PATTERNS)

    def _looks_like_verification_failure(self, text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in self.VERIFICATION_FAILURE_PATTERNS)

    def observe_tool_result(self, tool_name: str, tool_args: dict, result: ToolResult, runtime_state) -> None:
        if runtime_state is None:
            return
        if tool_failed(result):
            text = result_text(result)
            self._register_failure(text, runtime_state)
            if self._is_env_failure(text) and runtime_state.recovery.repeat_count >= 2:
                self._set_mode(runtime_state, "ENV_FIX")
                return
            if runtime_state.recovery.repeat_count >= 2:
                self._set_mode(runtime_state, "SPEC_RECHECK")
                return

        if tool_name in self.ACTION_TOOLS and result.status == "success":
            runtime_state.recovery.last_successful_action = tool_name

    def observe_verification_failure(self, failure_text: str, runtime_state) -> None:
        if runtime_state is None:
            return
        runtime_state.recovery.last_verification_result = failure_text
        self._register_failure(failure_text, runtime_state)
        if runtime_state.recovery.repeat_count >= 2:
            self._set_mode(runtime_state, "SPEC_RECHECK")

    def observe_edit_attempt(self, path: str, runtime_state) -> None:
        if runtime_state is None:
            return
        self._edit_attempts[path] = self._edit_attempts.get(path, 0) + 1
        if (
            runtime_state.recovery.mode == "SPEC_RECHECK"
            and runtime_state.recovery.failure_signature
            and self._edit_attempts[path] >= 2
        ):
            self._set_mode(runtime_state, "RETHINK")

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
        mode = runtime_state.recovery.mode
        if mode == "NORMAL":
            return None
        if tool_name == "update_plan_state":
            return None

        if mode == "PROBE":
            if tool_name not in self.ACTION_TOOLS:
                return None
            if runtime_state.recovery.probe_in_flight:
                return "[blocked] Recovery probe is already in flight; wait for its result before another action."
            if tool_name == "run_bash" and is_read_only_command(tool_args.get("command", "")):
                return None
            runtime_state.recovery.mode = "NORMAL"
            runtime_state.task_board.requires_update = False
            return None

        if mode == "ENV_FIX":
            if tool_name in {"write_file", "delegate_agent"}:
                return "[blocked] Recovery mode ENV_FIX only allows diagnosis, installation, and environment repair actions."
            return None

        if mode == "SPEC_RECHECK":
            if tool_name in {"write_file", "delegate_agent"}:
                return "[blocked] Recovery mode SPEC_RECHECK is read-only. Re-read the task and verification outputs first."
            if tool_name == "run_bash" and not is_read_only_command(tool_args.get("command", "")):
                return "[blocked] Recovery mode SPEC_RECHECK only allows read-only verification commands."
            return None

        if mode == "RETHINK":
            if tool_name in self.ACTION_TOOLS and runtime_state.task_board.requires_update:
                return "[blocked] Recovery mode RETHINK requires update_plan_state before more edits or commands."
            return None

        if mode == "FINAL_VERIFY":
            if tool_name in {"delegate_agent", "web_search", "web_fetch"}:
                return "[blocked] Recovery mode FINAL_VERIFY only allows direct verification and final fixes."
            return None

        return None

    def on_tool_allowed(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> None:
        if (
            agent_name in MAIN_AGENT_NAMES
            and runtime_state is not None
            and runtime_state.recovery.mode == "PROBE"
            and tool_name == "run_bash"
            and is_read_only_command(tool_args.get("command", ""))
        ):
            runtime_state.recovery.probe_in_flight = True

    def post_tool(self, tool_name: str, tool_args: dict, result: ToolResult,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or runtime_state is None:
            return None
        if (
            tool_name == "update_plan_state"
            and tool_failed(result)
            and runtime_state.task_board.replan_required
        ):
            runtime_state.recovery.replan_attempt_count += 1
            if runtime_state.recovery.replan_attempt_count >= 3:
                runtime_state.fallback.request_stop(
                    reason="replan_deadlock",
                    limit_type="replan_attempts",
                    used=runtime_state.recovery.replan_attempt_count,
                    limit=3,
                    last_tool=tool_name,
                )
                return (
                    "[SYSTEM] Required replanning failed repeatedly. The turn was stopped "
                    "to avoid a recovery loop; report the blocker instead of continuing."
                )
            return None
        if runtime_state.recovery.mode == "PROBE" and tool_name == "run_bash":
            runtime_state.recovery.probe_in_flight = False
            text = result_text(result)
            if tool_failed(result) or tool_blocked(result) or self._looks_like_verification_failure(text):
                self._register_failure(text, runtime_state)
                self._set_mode(runtime_state, "SPEC_RECHECK")
                return (
                    "[SYSTEM] Recovery probe failed. Stop the resumed strategy, re-read the evidence, "
                    "and submit another update_plan_state replan before more edits or commands."
                )
            self._clear_mode(runtime_state)
            runtime_state.task_board.requires_update = False
            runtime_state.task_board.replan_required = False
            runtime_state.task_board.replan_reason = ""
            return "[SYSTEM] Recovery probe passed. Resume the replanned work."

        self.observe_tool_result(tool_name, tool_args, result, runtime_state)
        if result.status == "failed":
            return None

        if tool_name == "run_bash":
            command = tool_args.get("command", "")
            if (
                runtime_state.recovery.mode == "ENV_FIX"
                and command
                and not is_read_only_command(command)
            ):
                self._clear_mode(runtime_state)
            elif (
                classify_safe_shell_command(command) == "verify"
                and self._looks_like_verification_failure(result_text(result))
            ):
                self.observe_verification_failure(result_text(result), runtime_state)

        if tool_name == "update_plan_state" and runtime_state.recovery.mode == "SPEC_RECHECK":
            self._clear_mode(runtime_state)

        if tool_name == "write_file":
            self.observe_edit_attempt(tool_args.get("path", ""), runtime_state)
        return None
