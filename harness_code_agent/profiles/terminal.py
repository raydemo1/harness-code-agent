"""
Terminal task profile — optimized for Terminal-Bench-2.

Key constraints:
  - 30 min (1800s) hard timeout per task
  - Tasks are well-defined CLI problems, not open-ended
  - No UI, no browser testing needed
  - Correctness is binary: tests pass or fail

All tunable parameters are read via self.cfg.resolve(), so you can override
them without touching this file:

  # Via environment variables:
  PROFILE_TERMINAL_TASK_BUDGET=1800
  PROFILE_TERMINAL_LOOP_FILE_EDIT_THRESHOLD=4
  PROFILE_TERMINAL_TIME_WARN_THRESHOLD=0.45
  # Or via ProfileConfig in code:
  from harness_code_agent.profiles.base import ProfileConfig
  cfg = ProfileConfig(task_budget=1200)
  profile = TerminalProfile(cfg=cfg)
"""
from __future__ import annotations

from .base import BaseProfile, AgentConfig
from ..runtime.middlewares import (
    LoopDetectionMiddleware,
    PreExitVerificationMiddleware,
    TimeBudgetMiddleware,
    TaskTrackingEnforcementMiddleware,
    ErrorGuidanceMiddleware,
    RecoveryStrategyMiddleware,
)

class TerminalProfile(BaseProfile):

    # --- Default values (overridable via ProfileConfig or env vars) ---
    _DEFAULTS = {
        "task_budget": 1800,
        "loop_file_edit_threshold": 4,
        "loop_command_repeat_threshold": 3,
        "task_tracking_nudge_after": 8,
        "time_warn_threshold": 0.45,
        "time_critical_threshold": 0.75,
    }

    def _get(self, key: str):
        """Resolve a config value: env var > ProfileConfig > default."""
        return self.cfg.resolve(key, self.name(), self._DEFAULTS[key])

    def name(self) -> str:
        return "terminal"

    def description(self) -> str:
        return "Solve terminal/CLI tasks (Terminal-Bench-2 style)"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are the main agent for a terminal/CLI task. You own the entire loop from task understanding to final verification.

NON-INTERACTIVE MODE:
- NEVER ask clarifying questions. Assume the most reasonable interpretation and act.
- Only you may modify files, create tests, integrate results, and decide when to stop.
- Consultation sub-agents are read-only helpers. Use consult_subagent only for local investigation, parallel search, test design, or review.
- Do not treat consultation output as completed work. Read it, decide what to adopt, and perform all changes yourself.

CRITICAL RULES:
- Your primary action is run_bash. Execute commands instead of describing them.
- run_bash uses one persistent shell session for the whole run.
- Before substantive work, run a Planning Mode Self-Check and call update_planning_files. Action tools are blocked until you do.
- Use skip for <3 estimated tool calls, light for 3-5, full for >5. Only mention the mode to the user for light/full.
- In light mode, keep progress.md current. In full mode, keep task_plan.md, findings.md, and progress.md current.
- Follow task specifications literally: exact paths, exact formats, exact filenames.

WORK LOOP:
1. Inspect the repository and task requirements.
2. Maintain planning state through update_planning_files.
3. Use consult_subagent for read-only investigation, test ideas, broad search, or review when helpful.
4. Make all code and test edits yourself with write_file.
5. Run tests or concrete checks with run_bash.
6. If checks fail, diagnose the evidence and fix.
7. Before stopping, verify each requirement against actual files or command output.

AVAILABLE TOOLS:
- run_bash: Execute shell commands.
- update_planning_files: Select skip/light/full mode and update planning files.
- write_file / read_file / list_files: File operations in the workspace.
- consult_subagent: Ask a read-only consultation helper for findings, evidence, recommendations, and risks.
- web_search / web_fetch: Search or fetch documentation when local context is insufficient.
- read_skill_file: Load a relevant skill guide.
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
                        "Switch to final review mode. Verify what ACTUALLY exists on disk. "
                        "Run concrete checks for every requirement. If any check fails, fix it before stopping."
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

    # --- TB2 task metadata for dynamic timeout ---
    _tb2_tasks: dict | None = None

    @classmethod
    def _load_tb2_tasks(cls) -> dict:
        """Load TB2 task metadata from bundled JSON."""
        if cls._tb2_tasks is None:
            import json
            from pathlib import Path
            tb2_path = Path(__file__).resolve().parents[2] / "benchmarks" / "tb2_tasks.json"
            if tb2_path.exists():
                cls._tb2_tasks = json.loads(tb2_path.read_text(encoding="utf-8"))
            else:
                cls._tb2_tasks = {}
        return cls._tb2_tasks

    def resolve_task_timeout(self, user_prompt: str) -> float | None:
        """Look up TB2 task timeout by matching task name in prompt or workspace path."""
        meta = self._lookup_task_meta(user_prompt)
        return meta.get("agent_timeout_sec") if meta else None

    def _lookup_task_meta(self, user_prompt: str) -> dict | None:
        """Look up full TB2 task metadata (timeout, difficulty, category)."""
        from .. import config as _cfg
        tasks = self._load_tb2_tasks()
        if not tasks:
            return None

        # Check workspace path first (most reliable)
        ws_lower = _cfg.WORKSPACE.lower()
        for task_name, meta in tasks.items():
            if task_name in ws_lower:
                return meta

        # Check user prompt
        prompt_lower = user_prompt.lower()
        for task_name, meta in tasks.items():
            if len(task_name) > 6 and (
                task_name in prompt_lower or
                task_name.replace("-", " ") in prompt_lower or
                task_name.replace("-", "_") in prompt_lower
            ):
                return meta

        return None
