from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .shell_classification import analyze_shell_command, classify_safe_shell_command

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
    "repo_search": TOOL_PERMISSION_READ,
    "tool_search": TOOL_PERMISSION_READ,
    "parallel_agents": TOOL_PERMISSION_READ,
    "parallel_commands": TOOL_PERMISSION_READ,
    "list_files": TOOL_PERMISSION_READ,
    "ask_user": TOOL_PERMISSION_READ,
    "memory_search": TOOL_PERMISSION_READ,
    "remember_memory": TOOL_PERMISSION_EDIT,
    "read_memory_file": TOOL_PERMISSION_READ,
    "delegate_agent": TOOL_PERMISSION_READ,
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
    VALID_MODES: ClassVar[set] = {WORKSPACE_WRITE, LLM_AUTO, DANGER_FULL_ACCESS}

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
        if risk in {"shell_risky", "unknown", "dangerous"}:
            return PermissionDecision(
                "ask",
                risk,
                f"{self.mode} mode requires approval for non-whitelisted commands and tools",
            )
        return PermissionDecision("allow", risk, f"{self.mode} mode allows {risk}")

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
            return analyze_shell_command(str(args.get("command", ""))).risk
        if permission == TOOL_PERMISSION_DANGEROUS:
            return "dangerous"
        return "unknown"


def is_read_only_command(command: str) -> bool:
    """Check whether a shell command is safe to run in read-only contexts."""
    return classify_safe_shell_command(command) in {"read", "verify"}
