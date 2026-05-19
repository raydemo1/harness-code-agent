"""
SWE-Bench profile — fix real GitHub issues in real repositories.
Analyze issue → locate code → write patch → run tests → iterate.
"""
from __future__ import annotations

from .base import BaseProfile, AgentConfig
from ..runtime.middlewares import (
    ErrorGuidanceMiddleware,
    LoopDetectionMiddleware,
    PreExitVerificationMiddleware,
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
    TimeBudgetMiddleware,
)


class SWEBenchProfile(BaseProfile):
    _DEFAULT_TASK_BUDGET = 3600

    def name(self) -> str:
        return "swe-bench"

    def description(self) -> str:
        return "Fix GitHub issues in real repositories (SWE-Bench style)"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are the main agent for a software bug-fix task. You own diagnosis, code changes, tests, verification, and the final stop decision.

Rules:
- Only you may modify files, create tests, integrate changes, and decide when the task is complete.
- Use consult_subagent only for read-only codebase investigation, test design, broad search, or review.
- Treat consultation output as advice. Read it, decide what to adopt, and perform all edits yourself.
- Make minimal, focused changes. Do not refactor unrelated code.
- Run concrete verification commands before stopping.

Workflow:
1. Read the issue or task carefully.
2. Inspect relevant files and tests.
3. Run a Planning Mode Self-Check and call update_planning_files before substantive work.
4. Consult read-only sub-agents if they can reduce risk or context load.
5. Modify the necessary source or test files yourself.
6. Run the relevant tests with run_bash.
7. If tests fail, use the output as evidence and fix the root cause.
8. Before stopping, review the diff and verify the acceptance criteria.
""",
            middlewares=[
                LoopDetectionMiddleware(),
                ErrorGuidanceMiddleware(),
                TaskTrackingEnforcementMiddleware(),
                RecoveryStrategyMiddleware(),
                PreExitVerificationMiddleware(
                    verification_prompt=(
                        "Review the actual diff and run the most relevant tests for the issue. "
                        "If tests fail, fix the root cause before stopping."
                    ),
                    include_task_requirements=True,
                ),
                TimeBudgetMiddleware(
                    budget_seconds=self.cfg.resolve("task_budget", self.name(), self._DEFAULT_TASK_BUDGET),
                ),
            ],
            time_budget=self.cfg.resolve("task_budget", self.name(), self._DEFAULT_TASK_BUDGET),
        )
