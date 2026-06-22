"""Planning profile with constrained planning-artifact writes."""
from __future__ import annotations

from .base import AgentConfig, BaseProfile, build_profile_prompt
from ..runtime.permissions import (
    TOOL_PERMISSION_CONTROL,
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_READ,
)


class PlanProfile(BaseProfile):
    _DEFAULT_TASK_BUDGET = 1800

    def name(self) -> str:
        return "plan"

    def description(self) -> str:
        return "Investigate and produce a decision-complete implementation plan"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt=build_profile_prompt(
                role=(
                    "Investigate the requested change and turn an uncertain idea into a decision-complete "
                    "implementation plan. The implementer should not need to rediscover repository facts "
                    "or make unresolved product decisions."
                ),
                working_style=(
                    "Explore first. Resolve file locations, existing patterns, interfaces, and constraints "
                    "from the repository or authoritative references before asking the user. Then ask focused "
                    "questions only about preferences or tradeoffs that evidence cannot decide. When several "
                    "high-impact choices remain, ask 2-3 related questions per round and continue until the "
                    "goal, boundaries, approach, failure handling, and acceptance criteria are stable.\n\n"
                    "Write the final plan around behavior and subsystem changes, naming concrete files, "
                    "symbols, tests, and commands where that specificity prevents ambiguity. Record any "
                    "remaining assumption explicitly rather than inventing certainty."
                ),
                boundaries=(
                    "This is a planning-only profile. Do not modify source, tests, dependencies, configuration, "
                    "migrations, or build outputs; do not run shell or browser actions; and do not implement any "
                    "part of the plan. Use update_plan_state for planning state and plan.md rather than direct "
                    "file writes. Do not create status.md or final.md."
                ),
                completion=(
                    "Finish only when the plan is decision-complete and includes a title, summary, implementation "
                    "changes, test plan, and explicit assumptions. If a material user decision is still missing, "
                    "ask for it instead of publishing a premature final plan."
                ),
            ),
            allowed_tool_permissions={
                TOOL_PERMISSION_READ,
                TOOL_PERMISSION_NETWORK_READ,
                TOOL_PERMISSION_CONTROL,
            },
            blocked_tool_names={"list_shell_jobs", "read_shell_output", "stop_shell_job"},
            middlewares=[],
            time_budget=self.cfg.resolve("task_budget", self.name(), self._DEFAULT_TASK_BUDGET),
        )
