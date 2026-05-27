from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class PermissionDecision:
    action: str
    risk: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @property
    def requires_approval(self) -> bool:
        return self.action == "ask"


class PermissionPolicy:
    """Runtime-enforced permission policy for tool calls."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"
    VALID_MODES = {READ_ONLY, WORKSPACE_WRITE, DANGER_FULL_ACCESS}
    READ_TOOLS = {
        "read_file",
        "read_skill_file",
        "list_files",
        "web_search",
        "web_fetch",
        "consult_subagent",
        "ask_user",
    }
    EDIT_TOOLS = {"write_file", "apply_patch", "update_plan_state"}

    def __init__(self, mode: str = WORKSPACE_WRITE):
        if mode not in self.VALID_MODES:
            raise ValueError(f"Unknown permission mode: {mode}")
        self.mode = mode

    def decide_tool_call(self, tool_name: str, args: dict | None = None) -> PermissionDecision:
        args = args or {}
        risk = self.classify_tool_call(tool_name, args)
        if self.mode == self.DANGER_FULL_ACCESS:
            return PermissionDecision("allow", risk, "danger-full-access mode allows this tool call")
        if self.mode == self.READ_ONLY:
            if risk == "read":
                return PermissionDecision("allow", risk, "read-only mode allows read tools")
            return PermissionDecision(
                "ask",
                risk,
                "read-only mode requires user approval for writes and shell commands",
            )
        if self.mode == self.WORKSPACE_WRITE:
            if risk == "shell_dangerous":
                return PermissionDecision(
                    "ask",
                    risk,
                    "workspace-write mode requires user approval for high-risk shell commands",
                )
            return PermissionDecision("allow", risk, f"workspace-write mode allows {risk}")
        return PermissionDecision("deny", risk, f"{self.mode} mode does not allow {risk}")

    def classify_tool_call(self, tool_name: str, args: dict) -> str:
        if tool_name in self.READ_TOOLS:
            return "read"
        if tool_name in self.EDIT_TOOLS:
            return "edit"
        if tool_name == "run_bash":
            return self.classify_shell_command(str(args.get("command", "")))
        return "unknown"

    def classify_shell_command(self, command: str) -> str:
        lowered = command.strip().lower()
        dangerous_patterns = [
            r"\brm\s+-[^\n;|&]*[rf]",
            r"\bgit\s+reset\s+--hard\b",
            r"\bgit\s+checkout\s+--\b",
            r"\bdel\s+/[qsf]",
            r"\bremove-item\b.*\b-recurse\b",
            r">(?!&)\s*\S+",
            r">>(?!&)\s*\S+",
            r"\bsed\s+-i\b",
            r"\bfind\b.*\b-delete\b",
            r"\bchmod\b",
            r"\bchown\b",
        ]
        if any(re.search(pattern, lowered) for pattern in dangerous_patterns):
            return "shell_dangerous"

        safe_prefixes = (
            "cat ", "type ", "ls", "dir", "pwd", "grep ", "rg ", "head ", "tail ",
            "git status", "git diff", "git log", "git show", "git branch",
            "python -m unittest", "python -m pytest", "pytest", "test ", "diff ",
            "wc ", "which ", "where ", "env",
        )
        if any(lowered.startswith(prefix) for prefix in safe_prefixes):
            return "shell_safe"

        risky_patterns = ("pip install", "npm install", "curl ", "wget ", "python ", "node ", "npm run")
        if any(lowered.startswith(prefix) for prefix in risky_patterns):
            return "shell_risky"
        return "shell_risky"
