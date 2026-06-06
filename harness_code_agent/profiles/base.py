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

from ..planning_policy import PLANNING_MODE_POLICY
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
    task_tracking_nudge_after: int | None = None      # tool calls before tracking nudge
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

    Subclass this to create a new scenario (app building, SWE-Bench,
    terminal tasks, etc.). The harness calls these methods to get
    scenario-specific configuration.

    Accepts an optional ProfileConfig for tunable parameters.
    Subclasses read config via self.cfg.resolve(field, profile_name, default).
    """

    def __init__(self, cfg: ProfileConfig | None = None):
        self.cfg = cfg or ProfileConfig()

    @abstractmethod
    def name(self) -> str:
        """Short identifier for this profile (e.g. 'app-builder', 'swe-bench')."""
        ...

    @abstractmethod
    def description(self) -> str:
        """One-line description shown in --help."""
        ...

    def main_agent(self) -> AgentConfig:
        """Config for the single owner agent that runs the full task loop."""
        prompt = (
            "You are the main agent for this task. You own the complete execution loop: "
            "understand the task, maintain progress, inspect the workspace, modify files, "
            "run verification, integrate all feedback, and decide when to stop.\n\n"
            "Sub-agents are consultation tools only. Use consult_subagent for local investigation, "
            "parallel search, test design, or review. They must not modify files, merge work, "
            "or decide whether the task is complete. Only you may modify files, create tests, "
            "perform final integration, and make the final stop decision.\n\n"
            "Required loop:\n"
            "1. Read the task and repository state.\n"
            f"2. {PLANNING_MODE_POLICY}\n"
            "3. Consult sub-agents only when their read-only findings would reduce context load or risk.\n"
            "4. Apply all code and test changes yourself.\n"
            "5. Run concrete verification commands.\n"
            "6. If verification fails, diagnose and continue. If it passes, update planning state when in light/full and perform final verification before stopping. "
            "Final light/full updates must include result_status, validation, and remaining_issues.\n"
            "Use read_shell_output, list_shell_jobs, and stop_shell_job to inspect and clean up any background shell jobs returned by long-running run_bash commands.\n\n"
            "Use the task text and profile acceptance criteria as the source of truth."
        )
        return AgentConfig(system_prompt=prompt)

    def subagent_policy(self) -> dict:
        """Policy for consultation-only sub-agents."""
        return {
            "allowed_scopes": [
                "codebase_investigation",
                "parallel_search",
                "test_design",
                "review",
            ],
            "read_only": True,
            "may_modify_files": False,
            "may_decide_completion": False,
        }

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
