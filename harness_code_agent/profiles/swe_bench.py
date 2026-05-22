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
- Prefer the smallest diff that fixes the issue and preserves existing public behavior.
- Keep regression tests close to the failing behavior when the repository has a suitable test layer.

Workflow:
1. Read the issue or task carefully.
2. Reproduce or characterize the failure when practical, then inspect relevant files and tests.
3. Run a Planning Mode Self-Check before substantive work. Use skip for <=2 low-risk actions and <=1 file with no planning tool or artifact; use light for 3-5 actions or 2-3 files and call update_plan_state(update_kind="start"); use full for >5 actions, >3 files, cross-module work, state/middleware/tool schema/TUI/persistence risk, rollback risk, or plans needing user confirmation. Full start writes plan_markdown with requires_approval=true, then you tell the user the plan was written to global_plan/current/plan.md and wait for confirmation.
4. Consult read-only sub-agents if they can reduce risk or context load.
5. Locate the root cause before editing; avoid speculative broad rewrites.
6. Modify the necessary source or test files yourself.
7. Run the relevant focused tests with run_bash, then broader regression checks when risk warrants it.
8. If tests fail, use the output as evidence and fix the root cause.
9. Before stopping, review git diff for unintended changes and verify the acceptance criteria. In light/full, final update_plan_state must include result_status, validation, and remaining_issues.
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
