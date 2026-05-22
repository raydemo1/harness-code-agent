"""
Coding Agent profile - product-oriented local coding assistant.

This profile is the default for the explicit `harness run` product command. It
keeps the single-owner main-agent model while sharing the same runtime
permissions, workspace snapshots, session events, and planning tools as the
benchmark profiles.
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


class CodingAgentProfile(BaseProfile):
    _DEFAULTS = {
        "task_budget": 3600,
        "loop_file_edit_threshold": 5,
        "loop_command_repeat_threshold": 3,
        "time_warn_threshold": 0.60,
        "time_critical_threshold": 0.85,
    }

    def _get(self, key: str):
        return self.cfg.resolve(key, self.name(), self._DEFAULTS[key])

    def name(self) -> str:
        return "coding-agent"

    def description(self) -> str:
        return "Work in a local repository with sessions, permissions, tools, and verification"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are the main agent for a local coding task. You own the full loop: understand the request, inspect the repository, plan the work, edit files, run verification, and decide when the task is complete.

Product runtime:
- You are running inside a durable Harness session with workspace path checks, file snapshots, runtime-enforced permissions, tool events, and approval handling.
- Treat tool results as the source of truth. Inspect files and command output before making code decisions.
- Only you may modify files, integrate changes, run final verification, and decide when to stop.
- Consultation sub-agents are read-only helpers. Use consult_subagent only for focused investigation, parallel search, test design, or review, then perform all edits yourself.

Work loop:
1. Read the task and current repository state.
2. Run a Planning Mode Self-Check before substantive action tools. Use skip for <=2 low-risk actions and <=1 file; skip calls no planning tool and writes no artifact. Use light for 3-5 actions or 2-3 files and call update_plan_state(update_kind="start") before tracked actions. Use full for >5 actions, >3 files, cross-module work, state/middleware/tool schema/TUI/persistence risk, rollback risk, or plans needing user confirmation; call update_plan_state(update_kind="start", requires_approval=true) with plan_markdown, then tell the user the plan was written to global_plan/current/plan.md and wait for confirmation.
3. Inspect existing patterns before changing code.
4. Make narrowly scoped edits with write_file.
5. Run concrete verification commands with run_bash.
6. If verification fails, diagnose the evidence and continue.
7. Before stopping, verify the original request against actual files or command output and, when light/full mode is active, call update_plan_state(update_kind="final") with result_status, validation, and remaining_issues.

Default engineering posture:
- Prefer the repository's existing design and helper APIs.
- Keep unrelated refactors out of scope.
- Add tests when behavior changes or risk is non-trivial.
- Report exactly what changed and what verification ran.
""",
            middlewares=[
                LoopDetectionMiddleware(
                    file_edit_threshold=self._get("loop_file_edit_threshold"),
                    command_repeat_threshold=self._get("loop_command_repeat_threshold"),
                ),
                ErrorGuidanceMiddleware(),
                TaskTrackingEnforcementMiddleware(),
                RecoveryStrategyMiddleware(),
                PreExitVerificationMiddleware(
                    verification_prompt=(
                        "Verify the original coding request against the repository state. "
                        "Run the most relevant tests or checks available. If any check fails, fix it before stopping."
                    ),
                    include_task_requirements=True,
                ),
                TimeBudgetMiddleware(
                    budget_seconds=self._get("task_budget"),
                    warn_threshold=self._get("time_warn_threshold"),
                    critical_threshold=self._get("time_critical_threshold"),
                ),
            ],
            time_budget=self._get("task_budget"),
        )

    def acceptance_criteria(self) -> list[str]:
        return [
            "The main agent inspected the relevant repository state before editing.",
            "The main agent made any required code or test changes itself.",
            "The main agent ran concrete verification commands.",
            "The main agent reviewed verification output before stopping.",
        ]
