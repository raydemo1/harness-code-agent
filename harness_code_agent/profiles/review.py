"""Review profile for read-only code review tasks."""
from __future__ import annotations

from .base import AgentConfig, BaseProfile, build_profile_prompt
from ..runtime.middleware import AgentMiddleware
from ..runtime.permissions import (
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_SHELL,
    is_read_only_command,
)
from ..runtime.tool_result import ToolResult


class ReviewOnlyMiddleware(AgentMiddleware):
    """Keep review mode read-only even if a blocked tool call slips through."""

    def __init__(self, *, profile_label: str = "review profile"):
        self._profile_label = profile_label

    _WRITE_OR_CONTROL_TOOLS = {
        "write_file",
        "apply_patch",
        "update_plan_state",
        "ask_user",
        "browser_test",
        "stop_dev_server",
        "stop_shell_job",
    }

    def before_tool(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> ToolResult | None:
        if tool_name in self._WRITE_OR_CONTROL_TOOLS:
            return ToolResult(
                tool=tool_name,
                status="failed",
                output=f"[blocked] The {self._profile_label} is read-only and must not modify files, control workflow state, ask the user, or run browser sessions.",
                error=f"{self._profile_label} is read-only",
                metadata={"status_source": self._profile_label.replace(" ", "_")},
            )
        if tool_name == "run_bash":
            command = str(tool_args.get("command", ""))
            if not is_read_only_command(command):
                return ToolResult(
                    tool=tool_name,
                    status="failed",
                    output=f"[blocked] The {self._profile_label} only allows safe verification or read-only shell commands.",
                    error="only safe verification shell commands are allowed",
                    metadata={"status_source": self._profile_label.replace(" ", "_")},
                )
        return None


class ReviewProfile(BaseProfile):
    def name(self) -> str:
        return "review"

    def description(self) -> str:
        return "Read-only code review mode with findings-first output"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt=build_profile_prompt(
                role=(
                    "Act as an independent read-only reviewer. Evaluate the requested code or changes for "
                    "actionable defects and risks rather than retelling the implementation."
                ),
                working_style=(
                    "Inspect the relevant diff, code paths, tests, and safe command output. Ground every "
                    "finding in observable evidence and prioritize correctness, security, data loss, "
                    "regressions, missing tests, and maintainability. Use delegation when a second read-only "
                    "perspective or verification pass reduces blind spots.\n\n"
                    "Present findings first and order them by severity. Each finding should identify the "
                    "location when available, explain the evidence and impact, and give a concrete recommendation."
                ),
                boundaries=(
                    "Review mode is not repair mode or planning mode. Do not modify files, update planning "
                    "state, ask the user to choose an implementation direction, start browser sessions, or "
                    "run mutating shell commands. You cannot manage or stop shell jobs."
                ),
                completion=(
                    "Stop when the review surface has been examined deeply enough to support the findings. "
                    "If no actionable issue is found, say so plainly and identify residual risk, assumptions, "
                    "or tests that were not run."
                ),
            ),
            allowed_tool_permissions={
                TOOL_PERMISSION_READ,
                TOOL_PERMISSION_NETWORK_READ,
                TOOL_PERMISSION_SHELL,
            },
            blocked_tool_names={
                "write_file",
                "apply_patch",
                "update_plan_state",
                "ask_user",
                "browser_test",
                "stop_dev_server",
                "stop_shell_job",
            },
            middlewares=[ReviewOnlyMiddleware()],
        )

    def acceptance_criteria(self) -> list[str]:
        return [
            "The review stayed read-only and did not modify workspace files.",
            "Findings, if any, are listed first and ordered by severity.",
            "Each finding includes evidence, impact, and a concrete recommendation.",
            "If no issues were found, the response says so clearly and names residual risk or test gaps.",
        ]
