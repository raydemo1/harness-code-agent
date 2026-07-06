"""
App Builder profile for single-agent web app creation and browser verification.
"""
from __future__ import annotations

from ..tracking_policy import TASK_TRACKING_POLICY
from .base import BaseProfile, AgentConfig, build_execution_middlewares, build_profile_prompt
from ..runtime.middleware import (
    PreExitVerificationMiddleware,
)


_APP_BUILDER_SYSTEM = build_profile_prompt(
    role=(
        "Turn the user's product idea into a complete, working application. Own both implementation "
        "quality and the product judgment needed to make an underspecified interface coherent."
    ),
    working_style=(
        "Understand the intended audience, core interaction, and visual character before choosing the "
        "smallest suitable stack. A focused static experience may be one HTML file; use the repository's "
        "existing framework when present, and introduce a framework only when the behavior or project "
        "context justifies it. Do not default to React merely because the stack is unspecified.\n\n"
        f"{TASK_TRACKING_POLICY}\n\n"
        "Build complete behavior rather than a mock screenshot. Make deliberate choices about hierarchy, "
        "typography, color, spacing, and interaction instead of relying on generic component defaults. "
        "Use delegation for focused investigation, design critique, test ideas, review, verification, or "
        "isolated patch proposals, then integrate the decisions and code yourself. Run the application, inspect browser console output, exercise "
        "representative interactions, and check both mobile and desktop layouts."
    ),
    boundaries=(
        "You MUST create or modify actual source files; reading specifications or describing an app is not "
        "a deliverable. Do not leave stubs, placeholder-only behavior, or TODO scaffolding in place of the "
        "requested product. Do not install a new dependency when the same result is practical with the "
        "existing stack or platform."
    ),
    completion=(
        "The app is complete when the requested flows work in the running product, browser checks show no "
        "unresolved console errors, responsive behavior is credible, and basic accessibility is covered "
        "through semantic controls, labels, focusability, and readable contrast. If browser tooling is "
        "unavailable, run the strongest build and static checks available and report that limitation."
    ),
)


class AppBuilderProfile(BaseProfile):
    _DEFAULTS = {
        "task_budget": 3600,
        "loop_file_edit_threshold": 4,
        "loop_command_repeat_threshold": 3,
        "require_start_after_n_actions": 5,
        "acceptance_review_timeout": 10.0,
        "time_warn_threshold": 0.60,
        "time_critical_threshold": 0.85,
    }

    def _get(self, key: str):
        return self.cfg.resolve(key, self.name(), self._DEFAULTS[key])

    def name(self) -> str:
        return "app-builder"

    def description(self) -> str:
        return "Build complete web applications from a one-sentence prompt (Anthropic article scenario)"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt=_APP_BUILDER_SYSTEM,
            blocked_tool_names=set(),
            middlewares=build_execution_middlewares(
                task_budget=self._get("task_budget"),
                loop_file_edit_threshold=self._get("loop_file_edit_threshold"),
                loop_command_repeat_threshold=self._get("loop_command_repeat_threshold"),
                time_warn_threshold=self._get("time_warn_threshold"),
                time_critical_threshold=self._get("time_critical_threshold"),
                enforce_acceptance=True,
                require_start_after_n_actions=self._get("require_start_after_n_actions"),
                acceptance_review_timeout=self._get("acceptance_review_timeout"),
                extra_before_time_budget=[
                    PreExitVerificationMiddleware(
                        verification_prompt=(
                            "Verify the app against the original request. Run concrete checks, "
                            "and use browser_test when a browser UI is involved."
                        ),
                        include_task_requirements=True,
                    ),
                ],
            ),
            time_budget=self._get("task_budget"),
        )
