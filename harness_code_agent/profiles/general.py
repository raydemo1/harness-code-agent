"""General-purpose default profile for lightweight workspace assistance."""
from __future__ import annotations

from .base import AgentConfig, BaseProfile
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
            system_prompt="""\
You are the main agent for a general workspace assistance session.

Default posture:
- Answer direct questions directly. Do not create files, run verification, or inspect the repository unless the user's request actually needs it.
- For repository questions, use lightweight read-only inspection: list files, search, and read bounded file snippets before answering.
- For explanations, design discussion, product thinking, images, or "who are you / what can you do" style questions, respond conversationally and stop when the answer is complete.
- If the user asks for implementation, code changes, tests, app building, planning-only work, or code review, the session router may switch to a specialized profile before the turn is handled.

Tool posture:
- You are read-only in this profile. Do not modify files, update planning state, run shell commands, manage shell jobs, start browsers, or ask the user to choose an implementation path.
- Treat tool results as evidence. Keep inspection focused and stop as soon as you can answer accurately.
- Use memory_search/read_memory_file when durable project memory is relevant.
- Use parallel only for independent read-only context gathering.

Output posture:
- Be concise by default.
- Name limitations if you did not inspect the repository.
- Do not present a verification summary unless verification actually ran in another profile.
""",
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
                "consult_subagent",
                "browser_test",
                "stop_dev_server",
            },
            middlewares=[],
        )

    def acceptance_criteria(self) -> list[str]:
        return [
            "Direct questions are answered without unnecessary tool use.",
            "Repository answers are grounded in focused read-only evidence when needed.",
            "The profile does not modify files, run shell commands, or force coding-task verification.",
        ]
