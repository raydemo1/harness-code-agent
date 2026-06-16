"""Lightweight read-only profile for local eval metric tasks."""
from __future__ import annotations

from .base import AgentConfig, BaseProfile
from .review import ReviewOnlyMiddleware
from ..runtime.permissions import (
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_SHELL,
)


class BasicEvalProfile(BaseProfile):
    def name(self) -> str:
        return "basic-eval"

    def description(self) -> str:
        return "Lightweight read-only repository inspection for eval metrics"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are the main agent for a lightweight local eval task.

Your job is to inspect the repository just enough to answer the user's question accurately. This is a read-only measurement task, not a coding task, not a code review, and not a planning task.

Rules:
- Do not modify files, update planning state, ask the user, start browsers, or manage shell jobs.
- Do not use consult_subagent or any consultation sub-agents.
- Prefer repo_search for code/text search, list_files for file discovery, and bounded read_file for file reads.
- Use run_bash only for execution tasks such as tests/builds when explicitly needed.
- Do not run broad test suites unless the user explicitly asks for a test result.
- Stop as soon as you have the answer.
- Include exact file paths, function names, constants, or metric names when they are relevant.
- Keep the final answer concise.
""",
            allowed_tool_permissions={
                TOOL_PERMISSION_READ,
                TOOL_PERMISSION_SHELL,
            },
            allowed_tool_names={
                "read_file",
                "repo_search",
                "list_files",
                "memory_search",
                "read_memory_file",
                "run_bash",
            },
            blocked_tool_names={
                "apply_patch",
                "ask_user",
                "browser_test",
                "consult_subagent",
                "list_shell_jobs",
                "read_shell_output",
                "read_skill_file",
                "stop_dev_server",
                "stop_shell_job",
                "tool_search",
                "update_plan_state",
                "web_fetch",
                "web_search",
                "write_file",
            },
            middlewares=[ReviewOnlyMiddleware(profile_label="basic-eval profile")],
        )

    def acceptance_criteria(self) -> list[str]:
        return [
            "The eval task stayed read-only.",
            "The answer includes the exact repository identifiers needed by the task.",
            "The agent stopped after answering without extra verification loops.",
        ]
