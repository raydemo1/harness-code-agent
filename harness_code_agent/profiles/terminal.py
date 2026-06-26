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

from .base import BaseProfile, AgentConfig, build_profile_prompt
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
            system_prompt=build_profile_prompt(
                role=(
                    "Solve a bounded terminal or CLI task under non-interactive benchmark conditions. "
                    "Translate the specification into observable acceptance checks, execute the work, "
                    "and close the loop with command evidence."
                ),
                working_style=(
                    "This profile starts every task in light planning mode. Before the first action tool, "
                    "call update_plan_state(update_kind=\"start\") with 1-10 concrete acceptance_checks. "
                    "Each check needs text, a short source grounded in the task, and a verification_command; "
                    "use manual only when command verification is impossible. Do not use echo/no-op commands "
                    "or 'checked by design' as verification; semantic constraints need scripts, diffs, greps, "
                    "or tests that can fail. Make the start plan verification-first: the steps should first "
                    "restate exact deliverables and constraints, identify likely hidden-verifier risks, design "
                    "the validation approach for those risks, then implement and rerun the acceptance commands. "
                    "Checks should cover observable success criteria and important negative constraints from "
                    "the task text; avoid checks that only prove a visible helper, one sample input, or a local "
                    "implementation detail. Track the framework-assigned acceptance_revision and give a reason "
                    "for every later add, update, or removal. On every replan, decide whether the current "
                    "acceptance checks still validate the new strategy; if not, update them in the same replan.\n\n"
                    f"{PLANNING_MODE_POLICY}\n\n"
                    "Follow specifications literally, especially exact paths, filenames, formats, and exact output. "
                    "Prefer command-driven evidence and syntax that matches the active shell. run_bash uses one "
                    "persistent shell session, so preserve useful cwd and environment state deliberately. For "
                    "background services, verify readiness separately and capture the exact process, port, or "
                    "path evidence. When a command fails, read stdout and stderr, classify the failure, and "
                    "switch strategy rather than retrying blindly. When one issue pattern appears in many "
                    "places, prefer a shell-driven batch workflow: search the full workspace, dry-run or print "
                    "the planned changes, apply a small deterministic migration script, then run one convergence "
                    "check that can fail. Keep iterating until the repository-wide search reports no remaining "
                    "active occurrences."
                ),
                boundaries=(
                    "NEVER ask clarifying questions in this non-interactive profile; choose the most reasonable "
                    "interpretation supported by the task and repository. Use write_file or apply_patch for "
                    "normal intentional source, test, configuration, or documentation edits. Do not edit through "
                    "ad-hoc shell redirection or opaque mutation commands. Exception: for many same-pattern "
                    "mechanical edits, you may use run_bash to execute a visible, deterministic migration script "
                    "inside the workspace after first printing or dry-running the intended file set; constrain it "
                    "to the task workspace and follow it with a repository-wide convergence check. Consultation "
                    "is read-only advice, not completed work."
                ),
                completion=(
                    "Verify every active acceptance check and include the latest acceptance_revision plus one "
                    "check_result per active check in the final planning update. Declare success only when every "
                    "check passed, the last foreground run_bash succeeded, and a successful foreground command "
                    "ran after the final structured file edit."
                ),
            ),
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
