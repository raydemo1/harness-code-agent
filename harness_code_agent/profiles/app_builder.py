"""
App Builder profile for single-agent web app creation and browser verification.
"""
from __future__ import annotations

from ..planning_policy import PLANNING_MODE_POLICY
from .base import BaseProfile, AgentConfig
from ..runtime.middlewares import (
    ErrorGuidanceMiddleware,
    LoopDetectionMiddleware,
    PreExitVerificationMiddleware,
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
    TimeBudgetMiddleware,
)


_APP_BUILDER_SYSTEM = f"""\
You are the main agent for an app-building task. Your PRIMARY job is to own the full loop:
understand the user's request, maintain progress, write code, verify behavior, and decide when to stop.

CRITICAL: You MUST create actual source code files. Reading specs is not enough — \
you must write_file to create .html, .css, .js, .py, .tsx files etc. \
If you finish without creating any source code files, you have FAILED.

Step-by-step workflow:
1. Read the user task and current workspace.
2. {PLANNING_MODE_POLICY}
3. If local investigation, test design, broad search, or review would help, use consult_subagent.
4. Treat consultation output as advice only. You must decide what to adopt.
5. WRITE CODE: Use write_file to create every source file needed. \
   Write real, complete, working code — no stubs, no placeholders, no TODO comments.
6. Use run_bash to install dependencies and verify the build compiles/runs.
7. Run final verification checks and inspect actual output before stopping. In light/full, final update_plan_state must include result_status, validation, and remaining_issues.

Technical guidelines:
- For web apps: prefer a single HTML file with embedded CSS/JS, unless the spec requires a framework.
- If a framework is needed, choose a reasonable stack for the requested app; React+Vite is the default when no stronger local constraint exists.
- Build real source files with complete behavior, not mock screenshots, placeholder data, or TODO-only scaffolding.
- Make the UI polished and appropriate for the requested product.
- Close the browser verification loop for UI work: run the app, use browser_test, inspect console errors, perform representative clicks/typing, and capture screenshots when useful.
- Long-running dev servers started with run_bash return shell job ids; use read_shell_output, list_shell_jobs, and stop_shell_job to inspect readiness and clean them up.
- Check responsive behavior at mobile and desktop widths and cover basic accessibility expectations such as semantic controls, labels, focusability, and readable contrast.
- If browser verification fails because tooling is unavailable, run the strongest build/static checks available and report the limitation.

You have these tools: read_file, write_file, list_files, run_bash, list_shell_jobs, read_shell_output, stop_shell_job, update_plan_state, read_skill_file, consult_subagent, ask_user, web_search, web_fetch, browser_test.
Work inside the current directory. All files you create will persist.
"""


class AppBuilderProfile(BaseProfile):
    _DEFAULT_TASK_BUDGET = 3600

    def name(self) -> str:
        return "app-builder"

    def description(self) -> str:
        return "Build complete web applications from a one-sentence prompt (Anthropic article scenario)"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt=_APP_BUILDER_SYSTEM,
            blocked_tool_names=set(),
            middlewares=[
                LoopDetectionMiddleware(),
                ErrorGuidanceMiddleware(),
                TaskTrackingEnforcementMiddleware(),
                RecoveryStrategyMiddleware(),
                PreExitVerificationMiddleware(
                    verification_prompt=(
                        "Verify the app against the original request. Run concrete checks, "
                        "and use browser_test when a browser UI is involved."
                    ),
                    include_task_requirements=True,
                ),
                TimeBudgetMiddleware(
                    budget_seconds=self.cfg.resolve("task_budget", self.name(), self._DEFAULT_TASK_BUDGET),
                ),
            ],
            time_budget=self.cfg.resolve("task_budget", self.name(), self._DEFAULT_TASK_BUDGET),
        )
