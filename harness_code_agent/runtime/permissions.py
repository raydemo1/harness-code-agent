from __future__ import annotations

import re
from dataclasses import dataclass

from .shell_classification import classify_safe_shell_command


TOOL_PERMISSION_READ = "read"
TOOL_PERMISSION_NETWORK_READ = "network_read"
TOOL_PERMISSION_EDIT = "edit"
TOOL_PERMISSION_CONTROL = "control"
TOOL_PERMISSION_SHELL = "shell"
TOOL_PERMISSION_DANGEROUS = "dangerous"
VALID_TOOL_PERMISSIONS = {
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_EDIT,
    TOOL_PERMISSION_CONTROL,
    TOOL_PERMISSION_SHELL,
    TOOL_PERMISSION_DANGEROUS,
}
DEFAULT_TOOL_PERMISSIONS = {
    "read_file": TOOL_PERMISSION_READ,
    "read_skill_file": TOOL_PERMISSION_READ,
    "list_files": TOOL_PERMISSION_READ,
    "ask_user": TOOL_PERMISSION_READ,
    "memory_search": TOOL_PERMISSION_READ,
    "remember_memory": TOOL_PERMISSION_EDIT,
    "read_memory_file": TOOL_PERMISSION_READ,
    "consult_subagent": TOOL_PERMISSION_READ,
    "web_search": TOOL_PERMISSION_NETWORK_READ,
    "web_fetch": TOOL_PERMISSION_NETWORK_READ,
    "write_file": TOOL_PERMISSION_EDIT,
    "apply_patch": TOOL_PERMISSION_EDIT,
    "update_plan_state": TOOL_PERMISSION_CONTROL,
    "run_bash": TOOL_PERMISSION_SHELL,
    "list_shell_jobs": TOOL_PERMISSION_READ,
    "read_shell_output": TOOL_PERMISSION_READ,
    "stop_shell_job": TOOL_PERMISSION_CONTROL,
}


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

    WORKSPACE_WRITE = "workspace-write"
    LLM_AUTO = "llm-auto"
    DANGER_FULL_ACCESS = "danger-full-access"
    VALID_MODES = {WORKSPACE_WRITE, LLM_AUTO, DANGER_FULL_ACCESS}

    def __init__(self, mode: str = WORKSPACE_WRITE):
        if mode not in self.VALID_MODES:
            raise ValueError(f"Unknown permission mode: {mode}")
        self.mode = mode

    def decide_tool_call(
        self,
        tool_name: str,
        args: dict | None = None,
        tool_permission: str | None = None,
    ) -> PermissionDecision:
        args = args or {}
        risk = self.classify_tool_call(tool_name, args, tool_permission=tool_permission)
        if risk == "shell_blocked":
            return PermissionDecision("deny", risk, "blacklisted shell command is never allowed")
        if self.mode == self.DANGER_FULL_ACCESS:
            return PermissionDecision("allow", risk, "danger-full-access mode allows this tool call")
        if self.mode == self.WORKSPACE_WRITE:
            if risk in {"shell_risky", "unknown", "dangerous"}:
                return PermissionDecision(
                    "ask",
                    risk,
                    "workspace-write mode requires user approval for non-whitelisted commands and tools",
                )
            return PermissionDecision("allow", risk, f"workspace-write mode allows {risk}")
        if self.mode == self.LLM_AUTO:
            if risk in {"shell_risky", "unknown", "dangerous"}:
                return PermissionDecision(
                    "ask",
                    risk,
                    "llm-auto mode requires automatic LLM approval for non-whitelisted commands and tools",
                )
            return PermissionDecision("allow", risk, f"llm-auto mode allows {risk}")
        return PermissionDecision("deny", risk, f"{self.mode} mode does not allow {risk}")

    def classify_tool_call(
        self,
        tool_name: str,
        args: dict,
        tool_permission: str | None = None,
    ) -> str:
        permission = tool_permission or DEFAULT_TOOL_PERMISSIONS.get(tool_name)
        if permission == TOOL_PERMISSION_READ:
            return "read"
        if permission == TOOL_PERMISSION_NETWORK_READ:
            return "network_read"
        if permission == TOOL_PERMISSION_EDIT:
            return "edit"
        if permission == TOOL_PERMISSION_CONTROL:
            return "control"
        if permission == TOOL_PERMISSION_SHELL:
            return self.classify_shell_command(str(args.get("command", "")))
        if permission == TOOL_PERMISSION_DANGEROUS:
            return "dangerous"
        return "unknown"

    def classify_shell_command(self, command: str) -> str:
        lowered = command.strip().lower()
        blocked_patterns = [
            r"\brm\s+-[^\n;|&]*[rf][^\n;|&]*(?:\s+--[^\n;|&]+)*\s+(?:/|/\*|~|~/\*|\.|\./\*|\*)\s*$",
            r"\bremove-item\b(?=.*-recurse\b)(?=.*(?:\bc:\\(?:\s|$)|\$home\b|~|(?:^|\s)\.(?:\s|$)|(?:^|\s)\*))",
            r"\bdel\b(?=.*(?:/[^\s]*s|-recurse\b))(?=.*(?:\bc:\\\*|\$home\b|~|(?:^|\s)\*))",
            r"\bmkfs(?:\.[\w-]+)?\b",
            r"(?:^|[;&|]\s*)format(?:\.com)?(?:\s|$)",
            r"\bdiskpart\b",
            r"\bdd\b.*\bof=/dev/",
        ]
        if any(re.search(pattern, lowered) for pattern in blocked_patterns):
            return "shell_blocked"
        if all(fragment in lowered for fragment in (":(){", ":|:&", "};:")):
            return "shell_blocked"

        if is_read_only_command(lowered):
            return "shell_safe"

        return "shell_risky"


def is_read_only_command(command: str) -> bool:
    """Check whether a shell command is safe to run in read-only contexts."""
    return classify_safe_shell_command(command) in {"read", "verify"}
