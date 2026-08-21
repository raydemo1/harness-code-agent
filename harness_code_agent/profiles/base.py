"""Base profile interface for main-agent task modes.

A Profile encapsulates everything scenario-specific:
  - The main-agent prompt
  - Extra tools and middleware for that agent
  - Acceptance criteria and task-specific timeout metadata

Configuration hierarchy (highest priority wins):
  1. Environment variables: PROFILE_<PROFILE_NAME>_<KEY> (e.g. PROFILE_TERMINAL_TASK_BUDGET=1200)
  2. ProfileConfig passed to constructor
  3. Profile subclass defaults
  4. BaseProfile defaults
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..tracking_policy import TASK_TRACKING_POLICY
from ..runtime.middleware import (
    AcceptanceReviewMiddleware,
    ErrorGuidanceMiddleware,
    LoopDetectionMiddleware,
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
    TimeBudgetMiddleware,
)
from ..runtime.permissions import (
    TOOL_PERMISSION_CONTROL,
    TOOL_PERMISSION_EDIT,
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_SHELL,
)


DEFAULT_PROFILE_TOOL_PERMISSIONS = {
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_EDIT,
    TOOL_PERMISSION_CONTROL,
    TOOL_PERMISSION_SHELL,
}
DEFAULT_PROFILE_BLOCKED_TOOLS = {"browser_test", "stop_dev_server"}


def build_profile_prompt(
    *,
    role: str,
    working_style: str,
    boundaries: str,
    completion: str,
) -> str:
    """Render profile-local behavior in one predictable, readable shape."""
    return (
        f"## Role\n{role.strip()}\n\n"
        f"## Working Style\n{working_style.strip()}\n\n"
        f"## Boundaries\n{boundaries.strip()}\n\n"
        f"## Completion\n{completion.strip()}"
    )


def build_execution_middlewares(
    *,
    task_budget: float,
    loop_file_edit_threshold: int,
    loop_command_repeat_threshold: int,
    time_warn_threshold: float = 0.60,
    time_critical_threshold: float = 0.85,
    enforce_acceptance: bool = False,
    require_start_after_n_actions: int | None = None,
    acceptance_review_timeout: float | None = None,
    extra_after_error: list | None = None,
    extra_before_time_budget: list | None = None,
) -> list:
    """Build the shared execution loop used by write-capable profiles."""
    middlewares = [
        LoopDetectionMiddleware(
            file_edit_threshold=loop_file_edit_threshold,
            command_repeat_threshold=loop_command_repeat_threshold,
        ),
        ErrorGuidanceMiddleware(),
    ]
    middlewares.extend(extra_after_error or [])
    if acceptance_review_timeout is not None:
        middlewares.append(AcceptanceReviewMiddleware(timeout_seconds=acceptance_review_timeout))
    middlewares.extend(
        [
            TaskTrackingEnforcementMiddleware(
                enforce_acceptance=enforce_acceptance,
                require_start_after_n_actions=require_start_after_n_actions,
            ),
            RecoveryStrategyMiddleware(),
        ]
    )
    middlewares.extend(extra_before_time_budget or [])
    middlewares.append(
        TimeBudgetMiddleware(
            budget_seconds=task_budget,
            warn_threshold=time_warn_threshold,
            critical_threshold=time_critical_threshold,
        )
    )
    return middlewares


@dataclass
class AgentConfig:
    """Configuration for the main agent."""
    system_prompt: str
    allowed_tool_permissions: set[str] = field(default_factory=lambda: set(DEFAULT_PROFILE_TOOL_PERMISSIONS))
    allowed_tool_names: set[str] = field(default_factory=set)
    blocked_tool_names: set[str] = field(default_factory=lambda: set(DEFAULT_PROFILE_BLOCKED_TOOLS))
    enabled: bool = True
    middlewares: list = field(default_factory=list)  # list[AgentMiddleware]
    time_budget: float | None = None  # seconds; None = no limit
    memory_enabled: bool = True
    initial_planning_mode: str = "unset"


@dataclass
class ProfileConfig:
    """
    Tunable parameters for a profile, separated from code.

    This lets you adjust thresholds, time budgets, and middleware settings
    without touching the profile's Python code. Useful for:
      - Rapid iteration on benchmark tuning
      - Per-model adjustments (different models need different settings)
      - A/B testing different configurations
    """
    # --- Time budgets (seconds) ---
    task_budget: float | None = None          # total time for the task

    # --- Middleware thresholds ---
    loop_file_edit_threshold: int | None = None      # edits before loop warning
    loop_command_repeat_threshold: int | None = None  # repeats before loop warning
    require_start_after_n_actions: int | None = None  # action tools before tracked start is required
    acceptance_review_timeout: float | None = None    # fast-model acceptance review timeout
    time_warn_threshold: float | None = None          # fraction of budget for warning
    time_critical_threshold: float | None = None      # fraction of budget for critical

    def _env_key(self, profile_name: str, field_name: str) -> str:
        """Build environment variable name: PROFILE_TERMINAL_TASK_BUDGET."""
        return f"PROFILE_{profile_name.upper().replace('-', '_')}_{field_name.upper()}"

    def resolve(self, field_name: str, profile_name: str, default):
        """
        Resolve a config value with priority: env var > explicit config > default.
        """
        # Check environment variable
        env_key = self._env_key(profile_name, field_name)
        env_val = os.environ.get(env_key)
        if env_val is not None:
            # Coerce to the type of default
            if isinstance(default, float):
                return float(env_val)
            elif isinstance(default, int):
                return int(env_val)
            return env_val

        # Check explicit config value
        config_val = getattr(self, field_name, None)
        if config_val is not None:
            return config_val

        return default


class BaseProfile(ABC):
    """
    Abstract base for task profiles.

    Subclass this to create a new scenario (app building, terminal tasks,
    review, etc.). The harness calls these methods to get
    scenario-specific configuration.

    Accepts an optional ProfileConfig for tunable parameters.
    Subclasses read config via self.cfg.resolve(field, profile_name, default).
    """

    def __init__(self, cfg: ProfileConfig | None = None):
        self.cfg = cfg or ProfileConfig()

    @abstractmethod
    def name(self) -> str:
        """Short identifier for this profile (e.g. 'app-builder', 'terminal')."""
        ...

    @abstractmethod
    def description(self) -> str:
        """One-line description shown in --help."""
        ...

    def main_agent(self) -> AgentConfig:
        """Config for the single owner agent that runs the full task loop."""
        prompt = build_profile_prompt(
            role=(
                "Own the complete task loop: understand the request, inspect the workspace, "
                "make the required changes, integrate useful delegated findings, verify the result, "
                "and decide when the work is complete."
            ),
            working_style=(
                "Begin from the task and current repository state. Use the planning policy below "
                "to match coordination overhead to risk, then follow existing project patterns and "
                "keep the implementation focused.\n\n"
                f"{TASK_TRACKING_POLICY}\n\n"
                "Use delegation only when independent investigation, test design, review, verification, "
                "or an isolated patch proposal would reduce risk or context load. Apply every code and "
                "test change to the real workspace yourself. Long-running "
                "shell commands return job IDs; inspect and clean them up through the shell-job tools."
            ),
            boundaries=(
                "The task text and profile acceptance criteria are the source of truth. Delegation "
                "is evidence or an isolated proposal, not completed work. Do not delegate real workspace "
                "modification, integration, final verification, or the stop decision."
            ),
            completion=(
                "Run concrete verification and read its output. If it fails, diagnose the evidence "
                "and continue. In tracked mode, finish with a final planning update "
                "that records result_status, validation, and remaining_issues."
            ),
        )
        return AgentConfig(system_prompt=prompt)

    def acceptance_criteria(self) -> list[str]:
        """High-level completion criteria for the main agent."""
        return [
            "The main agent made any required code or test changes itself.",
            "The main agent ran concrete verification commands.",
            "The main agent checked verification output before stopping.",
        ]

    def resolve_task_timeout(self, user_prompt: str) -> float | None:
        """
        Resolve the actual timeout for a task based on the user prompt.

        Override in subclasses that have task-specific timeout metadata
        (e.g. terminal profile uses TB2 task.toml data).

        Returns timeout in seconds, or None to use the default budget.
        """
        return None

    def resolve_task_metadata(self, user_prompt: str) -> dict | None:
        """
        Resolve profile-specific task metadata.

        Profiles can expose benchmark/task metadata to runtime middleware without
        making those middleware depend on a concrete benchmark launcher.
        """
        return None
