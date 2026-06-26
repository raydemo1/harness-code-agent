"""Task tracking middleware."""
from __future__ import annotations

import logging
import re

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
        missing_coverage = _missing_constraint_coverage(
            runtime_state.task_board.original_task,
            acceptance_snapshot["checks"],
        )
        if missing_coverage:
            return (
                "[blocked] success acceptance checks do not cover a task constraint: "
                f"{missing_coverage}. Add or update an acceptance check with a command that can fail."
            )
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


def _missing_constraint_coverage(task: str, checks: list[dict]) -> str:
    task_text = _normalize_constraint_text(task)
    check_text = _normalize_constraint_text(
        "\n".join(
            " ".join(
                str(check.get(field) or "")
                for field in ("text", "source", "verification_command")
            )
            for check in checks
        )
    )
    commands = _normalize_constraint_text(
        "\n".join(str(check.get("verification_command") or "") for check in checks)
    )
    combined = task_text + "\n" + check_text

    if _mentions_file_shape(combined) and not _commands_cover_file_shape(commands):
        return "exact file or directory shape must be verified with ls/find/test/stat or an assertion script"
    if _mentions_allowed_change(combined) and not _commands_cover_allowed_change(commands):
        return "allowed-change or preserve/do-not-modify constraints must be verified with diff, hash, cmp, git diff, or a custom assertion script"
    if _mentions_endpoint_or_protocol(combined) and not _commands_cover_endpoint_or_protocol(commands, task_text):
        return "literal endpoint, port, protocol, or service behavior must be verified with curl, grpc/protocol client, socket check, or service status command"
    if _mentions_generated_artifact(combined) and not _commands_cover_generated_artifact(commands):
        return "script-generated artifacts must be regenerated and validated for existence, format, or key invariants"
    return ""


def _normalize_constraint_text(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _mentions_file_shape(text: str) -> bool:
    patterns = (
        r"\bonly\b.*\b(?:file|files|directory|dir|contains?|exists?)\b",
        r"\b(?:single|exactly one|no extra|no additional)\b.*\b(?:file|files|artifact|artifacts)\b",
        r"\bdirectory\b.*\b(?:contains?|only|exactly)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _commands_cover_file_shape(commands: str) -> bool:
    patterns = (
        r"\bfind\b",
        r"\bls\b",
        r"\bstat\b",
        r"\btest\b",
        r"\b\[\s",
        r"\bos\.listdir\b",
        r"\bpathlib\b",
        r"\bassert\b.*\b(?:listdir|glob|exists|is_file|is_dir)\b",
    )
    return any(re.search(pattern, commands) for pattern in patterns)


def _mentions_allowed_change(text: str) -> bool:
    patterns = (
        r"\bonly\b.*\b(?:modify|change|replace|substitution|substitutions|edit|edits)\b",
        r"\b(?:allowed|valid)\b.*\b(?:substitution|substitutions|changes?|edits?)\b",
        r"\b(?:preserve|unchanged|do not modify|must not modify|without modifying)\b",
        r"\b(?:diff|token|tokens|schema|format)\b.*\b(?:preserve|unchanged|only|allowed)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _commands_cover_allowed_change(commands: str) -> bool:
    patterns = (
        r"\bgit\s+diff\b",
        r"\bdiff\b",
        r"\bcmp\b",
        r"\bsha(?:1|256)sum\b",
        r"\bmd5sum\b",
        r"\bhashlib\b",
        r"\bassert\b.*\b(?:diff|token|tokens|unchanged|allowed|preserve|modify|change)\b",
    )
    return any(re.search(pattern, commands) for pattern in patterns)


def _mentions_endpoint_or_protocol(text: str) -> bool:
    patterns = (
        r"https?://",
        r"\b(?:port|localhost|endpoint|url|http|https|grpc|ssh|server|service|socket)\b",
        r"\b(?:listen|serves?|responds?|protocol)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _commands_cover_endpoint_or_protocol(commands: str, task_text: str) -> bool:
    patterns = (
        r"\bcurl\b",
        r"\bgrpc\b",
        r"\bssh\b",
        r"\bnc\b",
        r"\bss\b",
        r"\blsof\b",
        r"\bsocket\b",
        r"\brequests\.",
        r"\burllib\b",
        r"\bsubprocess\b.*\b(?:curl|ssh|grpc)\b",
    )
    if not any(re.search(pattern, commands) for pattern in patterns):
        return False
    literals = _endpoint_literals(task_text)
    return all(literal in commands for literal in literals)


def _endpoint_literals(text: str) -> list[str]:
    literals: list[str] = []
    for match in re.finditer(r"https?://[^\s'\"),]+", text):
        url = match.group(0).rstrip(".,;:")
        literals.append(url)
        parsed = re.match(r"(https?)://([^/:]+)(?::(\d+))?(/[^?#\s]*)?", url)
        if parsed:
            literals.append(parsed.group(1))
            if parsed.group(3):
                literals.append(parsed.group(3))
            if parsed.group(4):
                literals.append(parsed.group(4))
    for match in re.finditer(r"\bport\s+(\d{2,5})\b|\blocalhost:(\d{2,5})\b|:(\d{2,5})\b", text):
        port = next(group for group in match.groups() if group)
        literals.append(port)
    for match in re.finditer(r"(?<!:)\/[a-z0-9._~\/-]+", text):
        path = match.group(0).rstrip(".,;:")
        if len(path) > 1:
            literals.append(path)
    return list(dict.fromkeys(literals))


def _mentions_generated_artifact(text: str) -> bool:
    patterns = (
        r"\b(?:script|program|command)\b.*\b(?:generat\w*|creat\w*|writ\w*|produc\w*|output\w*|sav\w*)\b",
        r"\b(?:generat\w*|produc\w*|output\w*|sav\w*)\b.*\b(?:json|csv|txt|npy|file|artifact|output)\b.*\b(?:script|program|command)\b",
        r"\b(?:run|execute)\b.*\b(?:script|program)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _commands_cover_generated_artifact(commands: str) -> bool:
    reruns_generator = any(
        re.search(pattern, commands)
        for pattern in (
            r"\b(?:python|python3|node|ruby|rscript|bash|sh)\b",
            r"\bmake\b",
            r"\bnpm\s+(?:run|test|exec)\b",
        )
    )
    validates_artifact = any(
        re.search(pattern, commands)
        for pattern in (
            r"\btest\s+-[fes]\b.*\.(?:json|csv|tsv|txt|npy|npz|parquet|pkl|pickle|png|jpg|jpeg|pdf|html|xml|yaml|yml)\b",
            r"\b(?:ls|stat)\b.*\.(?:json|csv|tsv|txt|npy|npz|parquet|pkl|pickle|png|jpg|jpeg|pdf|html|xml|yaml|yml)\b",
            r"\bjson\.load\b",
            r"\bnp\.load\b",
            r"\b(?:csv|pickle|yaml)\.",
            r"\bassert\b.*\b(?:exists|shape|json|load|format|output|artifact|generated|result)\b",
            r"\bpathlib\b.*\b(?:exists|is_file)\b",
        )
    )
    return reruns_generator and validates_artifact
