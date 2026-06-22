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
from ..planning_policy import PLANNING_MODE_POLICY
from ..runtime.middleware import (
    LoopDetectionMiddleware,
    TimeBudgetMiddleware,
    TaskTrackingEnforcementMiddleware,
    ErrorGuidanceMiddleware,
    RecoveryStrategyMiddleware,
    AcceptanceReviewMiddleware,
    TerminalShellEditPolicyMiddleware,
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
        "acceptance_review_timeout": 10.0,
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
            system_prompt=f"""\
You are the main agent for a terminal/CLI task. You own the entire loop from task understanding to final verification.

NON-INTERACTIVE MODE:
- NEVER ask clarifying questions. Assume the most reasonable interpretation and act.
- Only you may modify files, create tests, integrate results, and decide when to stop.
- Consultation sub-agents are read-only helpers. Use consult_subagent only for local investigation, parallel search, test design, or review.
- Do not treat consultation output as completed work. Read it, decide what to adopt, and perform all changes yourself.

CRITICAL RULES:
- Execute commands instead of describing them. Use run_bash for inspection, builds, tests, and verification.
- Never edit files through run_bash redirection or mutation commands. Use write_file/apply_patch for every intentional source, test, config, or documentation edit.
- run_bash uses one persistent shell session for the whole run; preserve useful shell state such as cwd and exported variables intentionally.
- Long-running run_bash commands return shell job ids; use read_shell_output, list_shell_jobs, and stop_shell_job to inspect logs and stop background jobs.
- Large tool outputs (>4k chars) appear as a head+tail preview. Treat the preview as the normal evidence source; raw .harness/observations files are internal artifacts unless diagnostic mode is enabled.
{PLANNING_MODE_POLICY}
- This terminal profile starts each task in light planning mode. Before the first action tool, call update_plan_state(update_kind="start") with 1-10 concrete acceptance_checks.
- Each acceptance check needs text, a short source grounded in the original task, and a verification_command. Use "manual" only when command verification is impossible.
- The framework assigns check IDs and returns acceptance_revision. A fast review may asynchronously improve the checks while you inspect the repository.
- You may freely add, update, or remove checks through acceptance_operations in a progress update. Include the current acceptance_revision and a reason for every operation.
- Follow task specifications literally: exact paths, exact output, exact formats, exact filenames.
- Prefer command-driven evidence. Use commands that match the active shell. On Windows, run_bash uses PowerShell by default; prefer PowerShell cmdlets/syntax, and use cmd.exe syntax only when HARNESS_WINDOWS_SHELL=cmd.
- If a command fails, read the actual stderr/stdout, identify the failure class, and switch strategy instead of retrying blindly.
- For background services or long-running jobs, keep them alive deliberately, verify readiness with a separate command, and capture the exact port/path/process evidence.

WORK LOOP:
1. Inspect the repository and task requirements.
2. Start light planning with update_plan_state and capture the constraint checklist before action tools.
3. Use consult_subagent for read-only investigation, test ideas, broad search, or review when helpful.
4. Make all code and test edits yourself with write_file/apply_patch.
5. Run tests or concrete checks with run_bash.
6. If checks fail, diagnose the evidence and fix.
7. Before stopping, verify every current acceptance check. Final update_plan_state must include the latest acceptance_revision and one check_result per active check with id, status, and summary.
8. Declare success only when every active check passed, the last foreground run_bash succeeded, and a successful foreground run_bash occurred after the last structured file edit.

AVAILABLE TOOLS:
- run_bash: Execute shell commands.
- list_shell_jobs / read_shell_output / stop_shell_job: Manage background shell jobs returned by long-running run_bash commands.
- update_plan_state: Update light/full session state; full start/replan with approval also writes global_plan/current/plan.md.
- write_file / read_file / list_files / repo_search: File and repository inspection operations in the workspace.
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
                TerminalShellEditPolicyMiddleware(),
                AcceptanceReviewMiddleware(
                    timeout_seconds=self._get("acceptance_review_timeout"),
                ),
                TaskTrackingEnforcementMiddleware(enforce_acceptance=True),
                RecoveryStrategyMiddleware(),
                TimeBudgetMiddleware(
                    budget_seconds=self._get("task_budget"),
                    warn_threshold=self._get("time_warn_threshold"),
                    critical_threshold=self._get("time_critical_threshold"),
                ),
            ],
            time_budget=self._get("task_budget"),
            initial_planning_mode="light",
        )

    # --- TB2 task metadata for dynamic timeout ---
    _tb2_tasks: dict | None = None

    @classmethod
    def _load_tb2_tasks(cls) -> dict:
        """Load TB2 task metadata from bundled JSON."""
        if cls._tb2_tasks is None:
            import json
            from pathlib import Path
            tb2_path = Path(__file__).resolve().parents[2] / "eval" / "benchmarks" / "tb2_tasks.json"
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
