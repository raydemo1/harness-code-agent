"""General-purpose default profile for lightweight workspace assistance."""
from __future__ import annotations

from .base import AgentConfig, BaseProfile, build_profile_prompt
from ..runtime.permissions import (
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_READ,
)


class GeneralProfile(BaseProfile):
    """Default profile for answer-first, mostly read-only work."""

    def name(self) -> str:
        return "general"

    def description(self) -> str:
        return "General workspace assistant for answers, discussion, and light read-only inspection"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt=build_profile_prompt(
                role=(
                    "Be the answer-first profile for ordinary questions, discussion, explanation, "
                    "and lightweight workspace understanding. A useful direct answer is often the "
                    "whole task."
                ),
                working_style=(
                    "Respond conversationally and concisely unless the subject genuinely needs more "
                    "structure. For repository questions, inspect only enough files or memory to ground "
                    "the answer. Prefer bounded reads and stop gathering context once the uncertainty "
                    "that matters is resolved. Use parallel_commands only for independent safe read-only "
                    "or verification commands that materially improve the answer.\n\n"
                    "Durable memory can replace redundant inspection when it gives exact, relevant "
                    "details; inspect the repository when memory is incomplete, contradictory, or "
                    "likely to have drifted."
                ),
                boundaries=(
                    "This profile is read-only. Do not modify files, run direct shell commands, manage jobs, "
                    "start browser sessions, update planning state, use delegated agents, or turn a discussion into an "
                    "implementation interview. Specialized implementation, planning, review, and app "
                    "work belongs in the corresponding profile."
                ),
                completion=(
                    "Stop when the question is answered accurately. Say when an answer was not grounded "
                    "in repository inspection, and never present a verification summary for checks that "
                    "did not run."
                ),
            ),
            allowed_tool_permissions={
                TOOL_PERMISSION_READ,
                TOOL_PERMISSION_NETWORK_READ,
            },
            blocked_tool_names={
                "write_file",
                "apply_patch",
                "remember_memory",
                "update_plan_state",
                "ask_user",
                "run_bash",
                "list_shell_jobs",
                "read_shell_output",
                "stop_shell_job",
                "delegate_agent",
                "parallel_agents",
                "browser_test",
                "stop_dev_server",
            },
            middlewares=[],
        )

    def acceptance_criteria(self) -> list[str]:
        return [
            "Direct questions are answered without unnecessary tool use.",
            "Repository answers are grounded in focused read-only evidence when needed.",
            "The profile does not modify files, run direct shell commands, use delegated agents, or force coding-task verification.",
        ]
