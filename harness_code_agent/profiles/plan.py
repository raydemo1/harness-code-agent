"""Read-only planning profile."""
from __future__ import annotations

from .base import AgentConfig, BaseProfile
from ..runtime.middlewares import ReadOnlyPlanningMiddleware
from ..runtime.tools import planning_tool_schemas


class PlanProfile(BaseProfile):
    _DEFAULT_TASK_BUDGET = 1800

    def name(self) -> str:
        return "plan"

    def description(self) -> str:
        return "Investigate read-only and produce a decision-complete implementation plan"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are the main agent for a read-only planning task. Your job is to investigate the repository, understand the requested change, and produce a decision-complete implementation plan.

Read-only contract:
- Do not modify files.
- Do not call write_file, apply_patch, update_planning_files, browser tools, package installation, service-starting commands, or git state-changing commands.
- Do not create task_plan.md, findings.md, progress.md, or any other planning artifact on disk.
- You may inspect files, list files, read skill files, run read-only shell commands, search/fetch references, and use consult_subagent for read-only sub-agent advice.
- Treat command and file output as evidence. If evidence is insufficient, state the assumption rather than inventing implementation details.

Planning standard:
- The plan must be decision-complete: an implementer should know what to edit, why, in what order, and how to verify it.
- Prefer concrete file paths, functions/classes, test names, command names, and expected behavior.
- Include risks and assumptions when the local evidence cannot fully decide something.
- Do not implement the plan.

Final answer format:
# Title

## Summary

## Implementation Changes

## Test Plan

## Assumptions
""",
            tool_schemas=planning_tool_schemas(),
            middlewares=[ReadOnlyPlanningMiddleware()],
            time_budget=self.cfg.resolve("task_budget", self.name(), self._DEFAULT_TASK_BUDGET),
        )
