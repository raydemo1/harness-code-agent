"""Review profile for read-only code review tasks."""
from __future__ import annotations

from .base import AgentConfig, BaseProfile
from ..runtime.middlewares import AgentMiddleware
from ..runtime.permissions import (
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_SHELL,
    is_read_only_command,
)
from ..runtime.tool_result import ToolResult


class ReviewOnlyMiddleware(AgentMiddleware):
    """Keep review mode read-only even if a blocked tool call slips through."""

    _WRITE_OR_CONTROL_TOOLS = {
        "write_file",
        "apply_patch",
        "update_plan_state",
        "ask_user",
        "browser_test",
        "stop_dev_server",
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
                output="[blocked] The review profile is read-only and must not modify files, control workflow state, ask the user, or run browser sessions.",
                error="review profile is read-only",
                metadata={"status_source": "review_profile"},
            )
        if tool_name == "run_bash":
            command = str(tool_args.get("command", ""))
            if not is_read_only_command(command):
                return ToolResult(
                    tool=tool_name,
                    status="failed",
                    output="[blocked] The review profile only allows safe verification or read-only shell commands.",
                    error="only safe verification shell commands are allowed",
                    metadata={"status_source": "review_profile"},
                )
        return None


class ReviewProfile(BaseProfile):
    def name(self) -> str:
        return "review"

    def description(self) -> str:
        return "Read-only code review mode with findings-first output"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are the main agent for a read-only code review task.

Your job is to inspect the repository, evaluate the requested code or changes, and report actionable review findings. Do not implement fixes. You must not modify files, update planning state, or ask the user to choose an implementation direction.

Allowed work:
- Read files, list files, read skill files, search/fetch references, and run safe verification or read-only shell commands.
- Use consult_subagent for focused read-only review help when it reduces risk or context load.
- Treat tool results as evidence. Ground every finding in actual files, diffs, command output, or explicit source material.

Output format:
- Findings first. Sort findings by severity.
- For each finding, include severity, file/line when available, evidence, impact, and a concrete recommendation.
- Prioritize correctness, security, data loss, regressions, missing tests, and maintainability risks.
- If no issues are found, state that clearly and mention any residual risk or tests not run.

Keep the review independent: review mode is not a repair mode, not a planning mode, and not a place to make workspace changes.
""",
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
