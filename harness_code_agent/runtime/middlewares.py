"""
Agent middlewares — hooks that run at specific points in the agent loop.

Middlewares are the harness engineer's primary tool for shaping agent behavior
without changing the core loop. They intercept execution at defined points:

  - post_tool:    After a tool call completes. Use for loop detection, tracking.
  - pre_exit:     When the agent wants to stop (no more tool calls). Use for
                  forced verification passes.
  - per_iteration: At the start of each iteration. Use for time budget warnings.

Middlewares return an optional message to inject into the conversation.
Returning None means "no intervention."

Design principle: middlewares are composable and profile-specific.
The base Agent loop knows nothing about terminal tasks or time budgets —
profiles wire in the middlewares they need.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .permissions import is_read_only_command

log = logging.getLogger("harness")


MAIN_AGENT_NAMES = {"main_agent"}


class AgentMiddleware(ABC):
    """Base class for agent middlewares."""

    def before_tool(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> str | None:
        """Called before each tool execution. Return a blocking message, or None."""
        return None

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        """Called after each tool execution. Return a message to inject, or None."""
        return None

    def pre_exit(self, messages: list[dict], runtime_state=None,
                 agent_name: str | None = None) -> str | None:
        """Called when the agent wants to stop. Return a message to force continuation, or None."""
        return None

    def per_iteration(self, iteration: int, messages: list[dict], runtime_state=None,
                      agent_name: str | None = None) -> str | None:
        """Called at the start of each iteration. Return a message to inject, or None."""
        return None

    def begin_turn(self, task: str, messages: list[dict], runtime_state=None,
                   agent_name: str | None = None) -> None:
        """Called before a new user turn is appended in a live conversation."""
        return None


# ---------------------------------------------------------------------------
# Loop Detection
# ---------------------------------------------------------------------------

class LoopDetectionMiddleware(AgentMiddleware):
    """
    Tracks per-file edit counts and detects repetitive command patterns.
    When the agent edits the same file or runs similar commands too many times,
    injects a nudge to reconsider the approach.

    Uses fuzzy matching for commands — catches variants like:
      python3 app.py  /  python3 app.py 2>&1  /  python3 ./app.py
    """

    def __init__(
        self,
        file_edit_threshold: int = 4,
        command_repeat_threshold: int = 3,
        tool_fingerprint_repeat_threshold: int = 3,
    ):
        self.file_edit_threshold = file_edit_threshold
        self.command_repeat_threshold = command_repeat_threshold
        self.tool_fingerprint_repeat_threshold = tool_fingerprint_repeat_threshold
        self.file_edit_counts: dict[str, int] = {}
        self.recent_commands: list[str] = []
        self.recent_tool_fingerprints: list[tuple[str, str]] = []
        self.recent_tool_summaries: list[str] = []
        self._fingerprint_warned: set[tuple[str, str]] = set()
        self._file_warned: set[str] = set()  # avoid spamming same warning

    def begin_turn(self, task: str, messages: list[dict], runtime_state=None,
                   agent_name: str | None = None) -> None:
        self.file_edit_counts.clear()
        self.recent_commands.clear()
        self.recent_tool_fingerprints.clear()
        self.recent_tool_summaries.clear()
        self._fingerprint_warned.clear()
        self._file_warned.clear()

    @staticmethod
    def _normalize_command(cmd: str) -> str:
        """Normalize a command for fuzzy comparison."""
        import re
        cmd = cmd.strip()
        # Remove common suffixes that don't change semantics
        cmd = re.sub(r'\s*2>&1\s*$', '', cmd)
        cmd = re.sub(r'\s*\|\s*head.*$', '', cmd)
        cmd = re.sub(r'\s*\|\s*tail.*$', '', cmd)
        # Normalize paths: ./foo → foo
        cmd = re.sub(r'\./(\S)', r'\1', cmd)
        # Collapse whitespace
        cmd = re.sub(r'\s+', ' ', cmd)
        return cmd.strip()

    @staticmethod
    def _fingerprint(tool_name: str, tool_args: dict) -> tuple[str, str]:
        normalized = json.dumps(tool_args or {}, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return tool_name, digest

    @staticmethod
    def _safe_args_preview(tool_args: dict) -> str:
        safe: dict[str, object] = {}
        for key, value in dict(tool_args or {}).items():
            key_text = str(key)
            try:
                value_text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            except TypeError:
                value_text = str(value)
            if key_text.lower() in {"content", "output", "text", "input", "code", "patch"} or len(value_text) > 120:
                safe[key_text] = f"[{len(value_text)} chars]"
            else:
                safe[key_text] = value
        return json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)[:220]

    def _observe_tool_fingerprint(self, tool_name: str, tool_args: dict, runtime_state=None) -> str | None:
        threshold = self.tool_fingerprint_repeat_threshold
        if threshold <= 1:
            return None
        fingerprint = self._fingerprint(tool_name, tool_args)
        summary = f"{tool_name}({self._safe_args_preview(tool_args)})"
        self.recent_tool_fingerprints.append(fingerprint)
        self.recent_tool_summaries.append(summary)
        self.recent_tool_fingerprints = self.recent_tool_fingerprints[-threshold:]
        self.recent_tool_summaries = self.recent_tool_summaries[-5:]

        fallback = getattr(runtime_state, "fallback", None)
        if fallback is not None:
            fallback.record_action(summary)

        if len(self.recent_tool_fingerprints) < threshold:
            return None
        if len(set(self.recent_tool_fingerprints[-threshold:])) != 1:
            return None

        if fingerprint in self._fingerprint_warned:
            if fallback is not None:
                fallback.request_stop(
                    reason="loop_detected",
                    limit_type="tool_fingerprint",
                    used=threshold + 1,
                    limit=threshold,
                    last_tool=tool_name,
                    fingerprint_hash=fingerprint[1],
                    recent_action_summary=self.recent_tool_summaries,
                )
            return None

        self._fingerprint_warned.add(fingerprint)
        log.warning("Loop detection: identical tool fingerprint repeated %sx", threshold)
        return (
            f"[SYSTEM] You have made the same tool call {threshold} times in a row.\n"
            f"Tool call pattern: {summary}\n"
            "This looks like a loop. Do not repeat it again. Change strategy, summarize what you know, "
            "or ask for a decision if the task is blocked."
        )

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        # Track file edits
        if tool_name == "write_file":
            path = tool_args.get("path", "")
            self.file_edit_counts[path] = self.file_edit_counts.get(path, 0) + 1
            count = self.file_edit_counts[path]
            if count >= self.file_edit_threshold and path not in self._file_warned:
                self._file_warned.add(path)
                log.warning(f"Loop detection: {path} edited {count} times")
                return (
                    f"[SYSTEM] You have edited '{path}' {count} times. "
                    "This pattern suggests your current approach may not be working. "
                    "STOP and reconsider:\n"
                    "1. Re-read the original task requirements.\n"
                    "2. Think about what's fundamentally wrong with your approach.\n"
                    "3. Try a completely different strategy."
                )

        # Track repeated commands (with fuzzy matching)
        if tool_name == "run_bash":
            cmd = tool_args.get("command", "").strip()
            self.recent_commands.append(cmd)
            if len(self.recent_commands) >= self.command_repeat_threshold:
                window = self.recent_commands[-self.command_repeat_threshold:]
                normalized = [self._normalize_command(c) for c in window]
                if len(set(normalized)) == 1:
                    log.warning(f"Loop detection: similar command repeated {self.command_repeat_threshold}x")
                    return (
                        f"[SYSTEM] You have run essentially the same command {self.command_repeat_threshold} "
                        f"times in a row with no progress.\n"
                        f"Command pattern: {normalized[0][:200]}\n"
                        "This is a doom loop. The same action will not produce a different result.\n"
                        "STOP. Re-read the error output carefully. Try a fundamentally different approach."
                    )

            # Also detect rapid-fire failed commands (different commands, same error)
            if "[error]" in result or "command not found" in result.lower():
                recent_errors = 0
                for msg in reversed(messages[-8:]):
                    content = msg.get("content", "")
                    if msg.get("role") == "tool" and (
                        "[error]" in content or "command not found" in content.lower()
                    ):
                        recent_errors += 1
                if recent_errors >= 3:
                    return (
                        "[SYSTEM] Multiple consecutive commands have failed. "
                        "Stop and diagnose the root cause before trying more commands. "
                        "Check: Is the required tool installed? Are you in the right directory? "
                        "Is there a dependency missing?"
                    )

        fingerprint_warning = self._observe_tool_fingerprint(tool_name, tool_args, runtime_state)
        if fingerprint_warning:
            return fingerprint_warning

        return None


# ---------------------------------------------------------------------------
# Pre-Exit Verification
# ---------------------------------------------------------------------------

class PreExitVerificationMiddleware(AgentMiddleware):
    """
    Forces the agent to run a verification pass before it's allowed to stop.

    Three-level exit gate:
    1. First exit attempt with NO tool calls ever made → force agent to start working
    2. First exit attempt after some work → force verification pass
    3. Second exit attempt after verification → allow exit

    This prevents the "3-second exit" problem where weak models return text
    without calling any tools, and PreExitVerification lets them go after
    just one retry.
    """

    def __init__(self, verification_prompt: str | None = None,
                 include_task_requirements: bool = True):
        self._exit_attempts = 0
        self._verification_prompt = verification_prompt
        self._include_task_requirements = include_task_requirements

    def begin_turn(self, task: str, messages: list[dict], runtime_state=None,
                   agent_name: str | None = None) -> None:
        self._exit_attempts = 0

    @staticmethod
    def _has_done_work(messages: list[dict], runtime_state=None) -> bool:
        """Check if the agent has called any action tools."""
        action_tools = {"run_bash", "write_file", "consult_subagent"}
        start = getattr(runtime_state, "current_turn_start_index", 0) if runtime_state is not None else 0
        for msg in messages[start:]:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn_name = tc.get("function", {}).get("name", "")
                    if fn_name in action_tools:
                        return True
        return False

    @staticmethod
    def _extract_task_requirements(messages: list[dict], runtime_state=None) -> str | None:
        """Extract the current turn's task requirements from the conversation."""
        start = getattr(runtime_state, "current_turn_start_index", 0) if runtime_state is not None else 0
        for msg in reversed(messages[start:]):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 20:
                    if len(content) > 3000:
                        content = content[:3000] + "\n... (truncated)"
                    return content
        return None

    def pre_exit(self, messages: list[dict], runtime_state=None,
                 agent_name: str | None = None) -> str | None:
        self._exit_attempts += 1
        has_worked = self._has_done_work(messages, runtime_state)

        # Gate 1: Agent hasn't done ANY work — force it to start
        if not has_worked:
            log.warning(f"Pre-exit: agent wants to stop but has done NO work (attempt {self._exit_attempts})")
            if self._exit_attempts <= 3:  # give up to 3 chances to start working
                return (
                    "[SYSTEM] You have NOT completed the task. You have not executed any commands "
                    "or written any files yet.\n"
                    "You MUST use run_bash to execute commands and write_file to create output files.\n"
                    "Read the task requirements again and START WORKING. Do not just describe "
                    "what you would do — actually DO it using the available tools."
                )
            # After 3 attempts with no work, give up
            log.error("Pre-exit: agent refused to work after 3 attempts")
            return None

        # Gate 2: Agent has done work, first exit → force verification
        if self._exit_attempts == 1:
            log.info("Pre-exit verification: forcing verification pass")

            parts = []
            parts.append(
                "[SYSTEM] MANDATORY VERIFICATION — You are about to finish, "
                "but you MUST verify your work first."
            )

            if self._include_task_requirements:
                task_text = self._extract_task_requirements(messages, runtime_state)
                if task_text:
                    parts.append(
                        "\n--- ORIGINAL TASK REQUIREMENTS (verify against these, not your memory) ---\n"
                        f"{task_text}\n"
                        "--- END ORIGINAL TASK REQUIREMENTS ---"
                    )

            if self._verification_prompt:
                parts.append(f"\n{self._verification_prompt}")
            else:
                parts.append(
                    "\nDo NOT just re-read your code. Run actual test/check commands:\n"
                    "1. Go through EACH requirement above one by one.\n"
                    "2. For each, run a concrete verification command "
                    "(cat, ls -la, test -f, diff, grep, python3 -c, etc.)\n"
                    "3. Compare ACTUAL output against what the task asked for.\n"
                    "4. Pay special attention to exact formats, column orders, "
                    "file paths, and edge-case rules mentioned in the task.\n"
                    "5. If ANY check fails, fix it before stopping.\n"
                    "Think like an automated test script — would your solution pass?"
                )

            return "\n".join(parts)

        # Gate 3: Agent has done work and verified → allow exit
        log.info("Pre-exit verification: agent verified, allowing exit")
        return None


# ---------------------------------------------------------------------------
# Time Budget
# ---------------------------------------------------------------------------

class TimeBudgetMiddleware(AgentMiddleware):
    """
    Injects time awareness into the agent loop.

    At configurable thresholds (default: 60% and 85% of budget),
    warns the agent about remaining time and nudges it toward
    wrapping up and verifying.

    Can track time from harness start (not just agent start) by calling
    sync_start_time() before the agent runs. This ensures the budget
    accounts for time already spent on planning/setup.
    """

    def __init__(self, budget_seconds: float,
                 warn_threshold: float = 0.60,
                 critical_threshold: float = 0.85):
        self.budget_seconds = budget_seconds
        self.warn_threshold = warn_threshold
        self.critical_threshold = critical_threshold
        self.start_time = time.time()
        self._warned = False
        self._critical = False

    def sync_start_time(self, harness_start: float):
        """Set start time to harness start, so budget includes planning/setup time."""
        self.start_time = harness_start

    def per_iteration(self, iteration: int, messages: list[dict], runtime_state=None,
                      agent_name: str | None = None) -> str | None:
        elapsed = time.time() - self.start_time
        fraction = elapsed / self.budget_seconds
        remaining = self.budget_seconds - elapsed

        if remaining <= 0:
            if not self._critical:
                self._critical = True
                log.warning("Time budget EXPIRED")
                return (
                    "[SYSTEM] ⚠️ TIME IS UP. You have exceeded the time budget.\n"
                    "STOP immediately. Save whatever you have and finish NOW."
                )
            return None

        if fraction >= self.critical_threshold and not self._critical:
            self._critical = True
            mins_left = remaining / 60
            log.warning(f"Time budget critical: {mins_left:.1f} min remaining")
            return (
                f"[SYSTEM] ⚠️ CRITICAL: Only {mins_left:.1f} minutes remaining out of "
                f"{self.budget_seconds / 60:.0f} min budget.\n"
                "STOP building new features. Immediately:\n"
                "1. Verify what you've done so far works correctly.\n"
                "2. Run final checks against the task requirements.\n"
                "3. Fix any broken items — do NOT start anything new."
            )

        if fraction >= self.warn_threshold and not self._warned:
            self._warned = True
            mins_left = remaining / 60
            log.info(f"Time budget warning: {mins_left:.1f} min remaining")
            return (
                f"[SYSTEM] Time check: {mins_left:.1f} minutes remaining out of "
                f"{self.budget_seconds / 60:.0f} min budget. "
                "Start wrapping up your current work and plan for verification."
            )

        return None


# ---------------------------------------------------------------------------
# Task Tracking (forced decomposition)
# ---------------------------------------------------------------------------

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
            board.actions_since_progress += 1
            if board.planning_mode in {"light", "full"}:
                board.needs_final_update = True
                if board.actions_since_progress >= 3:
                    board.actions_since_progress = 0
                    return (
                        "[SYSTEM] You have taken several tracked actions since the last planning update. "
                        "If the current step, completed steps, blockers, validation state, or next action changed, "
                        "write one consolidated update_plan_state progress update."
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
    ACTION_TOOLS = {"run_bash", "write_file", "apply_patch", "consult_subagent", "browser_test"}
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
        runtime_state.recovery.mode = mode
        runtime_state.task_board.requires_update = True

    def _clear_mode(self, runtime_state) -> None:
        runtime_state.recovery.mode = "NORMAL"
        runtime_state.recovery.failure_signature = ""
        runtime_state.recovery.repeat_count = 0

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

    def observe_tool_result(self, tool_name: str, tool_args: dict, result: str, runtime_state) -> None:
        if runtime_state is None:
            return
        if result.startswith("[error]"):
            self._register_failure(result, runtime_state)
            if self._is_env_failure(result) and runtime_state.recovery.repeat_count >= 2:
                self._set_mode(runtime_state, "ENV_FIX")
                return
            if runtime_state.recovery.repeat_count >= 2:
                self._set_mode(runtime_state, "SPEC_RECHECK")
                return

        if tool_name in self.ACTION_TOOLS and not result.startswith("[error]") and not result.startswith("[blocked]"):
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

        if mode == "ENV_FIX":
            if tool_name in {"write_file", "consult_subagent"}:
                return "[blocked] Recovery mode ENV_FIX only allows diagnosis, installation, and environment repair actions."
            return None

        if mode == "SPEC_RECHECK":
            if tool_name in {"write_file", "consult_subagent"}:
                return "[blocked] Recovery mode SPEC_RECHECK is read-only. Re-read the task and verification outputs first."
            if tool_name == "run_bash" and not is_read_only_command(tool_args.get("command", "")):
                return "[blocked] Recovery mode SPEC_RECHECK only allows read-only verification commands."
            return None

        if mode == "RETHINK":
            if tool_name in self.ACTION_TOOLS and runtime_state.task_board.requires_update:
                return "[blocked] Recovery mode RETHINK requires update_plan_state before more edits or commands."
            return None

        if mode == "FINAL_VERIFY":
            if tool_name in {"consult_subagent", "web_search", "web_fetch"}:
                return "[blocked] Recovery mode FINAL_VERIFY only allows direct verification and final fixes."
            return None

        return None

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or runtime_state is None:
            return None
        self.observe_tool_result(tool_name, tool_args, result, runtime_state)
        if result.startswith("[error]") or result.startswith("[blocked]"):
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
                is_read_only_command(command)
                and self._looks_like_verification_failure(result)
            ):
                self.observe_verification_failure(result, runtime_state)

        if tool_name == "update_plan_state" and runtime_state.recovery.mode == "SPEC_RECHECK":
            self._clear_mode(runtime_state)

        if tool_name == "write_file":
            self.observe_edit_attempt(tool_args.get("path", ""), runtime_state)
        return None




# ---------------------------------------------------------------------------
# Error Guidance (structured recovery for weak models)
# ---------------------------------------------------------------------------

class ErrorGuidanceMiddleware(AgentMiddleware):
    """
    Detects common error patterns in tool output and injects specific,
    actionable recovery suggestions.

    Weak models struggle to recover from errors on their own — they often
    retry the same failing command or give up. This middleware matches
    error patterns and provides concrete next steps.

    Based on TB2 command-level error analysis:
      - 24.1% of failures: command not found / not on PATH
      -  9.6% of failures: runtime errors in executables
      -  High rate: permission denied, missing dependencies
    """

    # Pattern → (description, recovery suggestion)
    # Patterns are checked in order; first match wins.
    ERROR_PATTERNS: list[tuple[str, str, str]] = [
        # --- Command not found ---
        (
            "command not found",
            "command_not_found",
            "The command is not installed. Try:\n"
            "  apt-get update && apt-get install -y <package>  (for system tools)\n"
            "  pip install <package>  (for Python tools)\n"
            "  which <command> || apt-cache search <keyword>  (to find the right package)\n"
            "If apt-get fails with permission denied, prefix with sudo.",
        ),
        (
            "no such file or directory",
            "file_not_found",
            "A file or directory doesn't exist. Check:\n"
            "  ls -la <parent_directory>  (does the path exist?)\n"
            "  pwd  (are you in the right directory?)\n"
            "  find . -name '<filename>'  (search for the file)",
        ),
        # --- Permission errors ---
        (
            "permission denied",
            "permission_denied",
            "Permission denied. Try:\n"
            "  chmod +x <file>  (if it needs to be executable)\n"
            "  sudo <command>  (if it needs root)\n"
            "  ls -la <file>  (check current permissions)",
        ),
        # --- Python/pip errors ---
        (
            "externally-managed-environment",
            "pip_managed_env",
            "This Python environment is externally managed (PEP 668). Use:\n"
            "  pip install --break-system-packages <package>\n"
            "  or: pip install --user <package>\n"
            "  or: python3 -m venv /tmp/venv && source /tmp/venv/bin/activate",
        ),
        (
            "modulenotfounderror",
            "python_import",
            "A Python module is missing. Install it:\n"
            "  pip install <module_name>\n"
            "  pip install --break-system-packages <module_name>  (if managed env)\n"
            "Check the exact package name — it may differ from the import name.",
        ),
        (
            "no module named",
            "python_import",
            "A Python module is missing. Install it:\n"
            "  pip install <module_name>\n"
            "Check: the pip package name may differ from the import name "
            "(e.g. 'import cv2' → 'pip install opencv-python').",
        ),
        # --- Compilation errors ---
        (
            "fatal error:",
            "compilation",
            "Compilation failed. Check:\n"
            "  1. Read the error — it shows the file and line number.\n"
            "  2. Missing header? Install dev packages: apt-get install -y lib<name>-dev\n"
            "  3. Use: apt-cache search <header_name> to find the right package.",
        ),
        (
            "undefined reference to",
            "linker",
            "Linker error — a symbol is missing. Check:\n"
            "  1. Are you linking all required libraries? (-l<lib> flag)\n"
            "  2. Is the library installed? apt-get install -y lib<name>-dev\n"
            "  3. Check library search path: ldconfig -p | grep <lib>",
        ),
        # --- Git errors ---
        (
            "not a git repository",
            "git",
            "Not in a git repository. Try:\n"
            "  git init  (to create one)\n"
            "  cd <correct_directory>  (you may be in the wrong dir)\n"
            "  find / -name '.git' -type d 2>/dev/null  (find existing repos)",
        ),
        # --- Disk/resource errors ---
        (
            "no space left on device",
            "disk_full",
            "Disk is full. Free space:\n"
            "  df -h  (check disk usage)\n"
            "  du -sh /* 2>/dev/null | sort -rh | head  (find large dirs)\n"
            "  apt-get clean  (clear package cache)\n"
            "  rm -rf /tmp/*  (clear temp files)",
        ),
        (
            "killed",
            "oom",
            "Process was killed (likely out of memory). Try:\n"
            "  free -h  (check available memory)\n"
            "  Reduce memory usage: smaller batch size, fewer workers, etc.\n"
            "  Use swap: fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile",
        ),
    ]

    def __init__(self):
        self._last_guidance_type: str | None = None

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        if tool_name != "run_bash":
            return None

        result_lower = result.lower()

        # Skip if no error indicators
        if "[error]" not in result_lower and "error" not in result_lower and "not found" not in result_lower:
            self._last_guidance_type = None
            return None

        for pattern, guidance_type, suggestion in self.ERROR_PATTERNS:
            if pattern in result_lower:
                # Don't repeat the same guidance type consecutively
                if guidance_type == self._last_guidance_type:
                    return None
                self._last_guidance_type = guidance_type
                log.info(f"Error guidance: matched '{guidance_type}'")
                return f"[SYSTEM] Error detected — here's how to fix it:\n{suggestion}"

        self._last_guidance_type = None
        return None



# ---------------------------------------------------------------------------
# Static Verifier (pre-exit lint gate, git-diff based)
# ---------------------------------------------------------------------------

VERDICT_PASS = 0
VERDICT_WARN = 1
VERDICT_BLOCK = 2


class StaticVerifierMiddleware(AgentMiddleware):
    """Pre-exit lint gate for Python files changed in the current turn.

    - ``py_compile`` (stdlib): syntax errors on any changed .py → block
    - ``ruff --diff`` (optional): only reports errors on *new/changed lines*,
      E/F → block, W/C/N → warn.  Gracefully skipped if ruff is not installed.
    """

    def __init__(self, workspace_root: str | None = None, workspace=None):
        self._workspace_root = workspace_root
        self._workspace = workspace
        self._turn_changed_start = len(getattr(workspace, "changed_files", [])) if workspace is not None else 0
        self._reported_warning_signatures: set[tuple[str, ...]] = set()

    def begin_turn(self, task: str, messages: list[dict], runtime_state=None,
                   agent_name: str | None = None) -> None:
        if self._workspace is not None:
            self._turn_changed_start = len(getattr(self._workspace, "changed_files", []))
        self._reported_warning_signatures.clear()

    def pre_exit(self, messages: list[dict], runtime_state=None,
                 agent_name: str | None = None) -> str | None:
        py_files = _turn_changed_py_files(
            self._workspace_root,
            self._workspace,
            self._turn_changed_start,
        )
        if not py_files:
            return None

        blocks: list[str] = []
        warns: list[str] = []

        # --- py_compile: syntax errors on changed files ---
        for path, msg in _check_py_compile(self._workspace_root, py_files):
            blocks.append(f"  [syntax] {path}: {msg}")

        # --- ruff --diff: only errors on changed lines, E/F → block ---
        for path, code, msg in _check_ruff_diff(self._workspace_root):
            line = f"  [{code}] {path}: {msg}" if path else f"  [{code}] {msg}"
            if code and code[0] in {"E", "F"}:
                blocks.append(line)
            else:
                warns.append(line)

        if blocks:
            details = "\n".join(blocks[:20])
            return (
                "[SYSTEM] LINT CHECK FAILED -- fix these errors before stopping:\n"
                f"{details}"
            )
        if warns:
            details = "\n".join(warns[:20])
            signature = tuple(warns[:20])
            if signature in self._reported_warning_signatures:
                return None
            self._reported_warning_signatures.add(signature)
            return (
                f"[SYSTEM] Lint warnings (non-blocking):\n{details}\n"
                "Consider fixing before stopping."
            )
        return None


# ------------------------------------------------------------------
# Git diff helpers
# ------------------------------------------------------------------

def _turn_changed_py_files(workspace_root: str | None, workspace, start_index: int) -> list[str]:
    if workspace is None:
        return _git_diff_changed_py_files(workspace_root)
    root = Path(workspace_root or getattr(workspace, "root", ".")).resolve()
    changed = getattr(workspace, "changed_files", [])[start_index:]
    files: set[str] = set()
    for path in changed:
        rel = Path(path)
        rel_text = rel.as_posix()
        if rel_text.endswith(".py") and (root / rel).exists():
            files.add(rel_text)
    return sorted(files)


def _git_diff_changed_py_files(workspace_root: str | None) -> list[str]:
    """Return .py files with uncommitted changes (tracked + untracked)."""
    import subprocess
    if not workspace_root:
        return []
    files: list[str] = []
    # Tracked changes (modified, added, renamed)
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=workspace_root,
        )
        if result.returncode == 0:
            files.extend(result.stdout.splitlines())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Untracked files
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
            cwd=workspace_root,
        )
        if result.returncode == 0:
            files.extend(result.stdout.splitlines())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return list({f.strip().replace("\\", "/") for f in files if f.strip().endswith(".py")})


# ------------------------------------------------------------------
# Checkers
# ------------------------------------------------------------------

def _check_py_compile(
    workspace_root: str | None, py_files: list[str],
) -> list[tuple[str, str]]:
    """Parse each file for syntax errors without writing bytecode."""
    import ast
    errors: list[tuple[str, str]] = []
    for rel_path in py_files:
        full_path = Path(workspace_root) / rel_path if workspace_root else Path(rel_path)
        try:
            source = full_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(source, filename=str(full_path))
        except (OSError, SyntaxError) as exc:
            errors.append((rel_path, str(exc)))
    return errors


def _check_ruff_diff(workspace_root: str | None) -> list[tuple[str, str, str]]:
    """Run ``ruff check --diff`` — only reports findings on changed lines."""
    import subprocess
    if not workspace_root:
        return []
    try:
        result = subprocess.run(
            ["ruff", "check", "--diff", "--no-fix", "--output-format=text"],
            capture_output=True, text=True, timeout=30,
            cwd=workspace_root,
        )
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        return [("", "TIMEOUT", "ruff check timed out after 30s")]
    if result.returncode == 0:
        return []
    findings: list[tuple[str, str, str]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(":", 3)
        if len(parts) >= 4:
            path = parts[0].strip()
            rest = parts[3].strip()
            code_end = rest.find(" ")
            if code_end > 0:
                code, msg = rest[:code_end], rest[code_end + 1:]
            else:
                code, msg = rest, ""
            findings.append((path, code, msg))
        else:
            findings.append(("", "", line))
    return findings
