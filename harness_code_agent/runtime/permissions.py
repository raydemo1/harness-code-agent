from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .shell_classification import (
    analyze_shell_command,
    is_workspace_write_shell_command,
)

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
        permission = tool_permission or _builtin_permission(tool_name)
        risk = self.classify_tool_call(tool_name, args, tool_permission=permission)
        if risk == "shell_blocked":
            return PermissionDecision(
                "deny", risk, "blacklisted shell command is never allowed"
            )
        if risk == "shell_write_blocked":
            return PermissionDecision(
                "deny",
                risk,
                "shell file deletion or overwrite is not allowed; use a controlled file edit after confirming a backup",
            )
        if (
            permission == TOOL_PERMISSION_SHELL
            and risk == "shell_risky"
            and self.mode != self.DANGER_FULL_ACCESS
        ):
            return PermissionDecision(
                "ask",
                risk,
                f"{self.mode} mode requires approval because this shell command may change files or external state",
            )
        if self.mode == self.DANGER_FULL_ACCESS:
            return PermissionDecision(
                "allow", risk, "danger-full-access mode allows this tool call"
            )
        if risk in {"shell_risky", "unknown", "dangerous"}:
            return PermissionDecision(
                "ask",
                risk,
                f"{self.mode} mode requires approval for commands or tools with side effects",
            )
        return PermissionDecision("allow", risk, f"{self.mode} mode allows {risk}")

    def classify_tool_call(
        self,
        tool_name: str,
        args: dict,
        tool_permission: str | None = None,
    ) -> str:
        permission = tool_permission or _builtin_permission(tool_name)
        if permission == TOOL_PERMISSION_READ:
            return "read"
        if permission == TOOL_PERMISSION_NETWORK_READ:
            return "network_read"
        if permission == TOOL_PERMISSION_EDIT:
            return "edit"
        if permission == TOOL_PERMISSION_CONTROL:
            return "control"
        if permission == TOOL_PERMISSION_SHELL:
            command = str(args.get("command", ""))
            analysis = analyze_shell_command(command)
            risk = "shell_risky" if analysis.kind == "unknown" else analysis.risk
            if risk == "shell_safe" and analysis.sensitive_read:
                return "shell_risky"
            return risk
        if permission == TOOL_PERMISSION_DANGEROUS:
            return "dangerous"
        return "unknown"


def is_workspace_write_command(command: str) -> bool:
    """Check only the workspace-write boundary, without a read command allowlist."""
    return is_workspace_write_shell_command(command)


def _builtin_permission(tool_name: str) -> str | None:
    from .builtins.registry import BUILTIN_TOOL_REGISTRY

    return BUILTIN_TOOL_REGISTRY.permission_for(tool_name)
