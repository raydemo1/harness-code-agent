"""
App Builder profile — the original scenario from the Anthropic article.
Plan a product → write code → browser-test → iterate.
"""
from __future__ import annotations

from .. import prompts
from ..runtime import tools
from .base import BaseProfile, AgentConfig
from ..runtime.middlewares import (
    ErrorGuidanceMiddleware,
    LoopDetectionMiddleware,
    PreExitVerificationMiddleware,
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
    TimeBudgetMiddleware,
)


class AppBuilderProfile(BaseProfile):
    _DEFAULT_TASK_BUDGET = 3600

    def name(self) -> str:
        return "app-builder"

    def description(self) -> str:
        return "Build complete web applications from a one-sentence prompt (Anthropic article scenario)"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt=prompts.BUILDER_SYSTEM,
            extra_tool_schemas=tools.BROWSER_TOOL_SCHEMAS,
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

    def planner(self) -> AgentConfig:
        return AgentConfig(system_prompt=prompts.PLANNER_SYSTEM)

    def builder(self) -> AgentConfig:
        return AgentConfig(system_prompt=prompts.BUILDER_SYSTEM)

    def evaluator(self) -> AgentConfig:
        return AgentConfig(
            system_prompt=prompts.EVALUATOR_SYSTEM,
            extra_tool_schemas=tools.BROWSER_TOOL_SCHEMAS,
        )

    def contract_proposer(self) -> AgentConfig:
        return AgentConfig(system_prompt=prompts.CONTRACT_BUILDER_SYSTEM)

    def contract_reviewer(self) -> AgentConfig:
        return AgentConfig(system_prompt=prompts.CONTRACT_REVIEWER_SYSTEM)
