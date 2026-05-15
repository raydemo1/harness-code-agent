#!/usr/bin/env python3
"""
Harness — profile-driven main-agent architecture for autonomous task execution.

The core loop is owned by one main agent. Consultation sub-agents are read-only
helpers for local investigation, parallel search, test design, and review.

Built-in profiles:
  app-builder  — Build web apps from a prompt (original Anthropic article scenario)
  terminal     — Solve terminal/CLI tasks (Terminal-Bench-2 style)
  swe-bench    — Fix GitHub issues in real repos
  reasoning    — Knowledge-intensive QA (MMMU-Pro style)

Usage:
  python harness.py "Build a DAW in the browser"                    # default: app-builder
  python harness.py --profile terminal "Fix the broken git merge"
  python harness.py --profile swe-bench "Fix issue #123"
  python harness.py --profile reasoning "Calculate the orbital period of..."
  python harness.py --list-profiles
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import config
import tools
from approvals import ConsoleApprovalProvider
from agents import Agent
from skills import SkillRegistry
from profiles import get_profile, list_profiles
from profiles.base import BaseProfile
from permissions import PermissionPolicy
from session import SessionStore
from tool_runtime import ToolContext
from workspace_service import WorkspaceService

log = logging.getLogger("harness")


class Harness:
    """
    Generic main-agent controller driven by a Profile.

    The Profile defines:
      - The main-agent system prompt
      - Which extra tools and middlewares the main agent gets
      - Consultation sub-agent policy
      - Acceptance criteria

    The Harness handles:
      - Workspace and git management
      - Starting exactly one main agent for the task
    """

    def __init__(self, profile: BaseProfile):
        self.profile = profile
        self.skill_registry = SkillRegistry()
        skill_catalog = self.skill_registry.build_catalog_prompt()
        self._main_cfg = profile.main_agent()
        self._skill_catalog = skill_catalog
        self.main_agent = self._build_main_agent()

    def _build_main_agent(self, tool_context: ToolContext | None = None) -> Agent:
        main_cfg = self._main_cfg
        kwargs = {
            "use_tools": True,
            "extra_tool_schemas": main_cfg.extra_tool_schemas,
            "middlewares": main_cfg.middlewares,
            "time_budget": main_cfg.time_budget,
        }
        if tool_context is not None:
            kwargs["tool_context"] = tool_context
        self.main_agent = Agent(
            "main_agent",
            main_cfg.system_prompt + self._skill_catalog,
            **kwargs,
        )
        return self.main_agent

    def run(self, user_prompt: str) -> None:
        # Create a unique project subdirectory under workspace
        # (skip if HARNESS_FLAT_WORKSPACE is set — used for benchmarks
        #  where outputs must land directly in the workspace root)
        if os.environ.get("HARNESS_FLAT_WORKSPACE"):
            Path(config.WORKSPACE).mkdir(parents=True, exist_ok=True)
        else:
            from datetime import datetime
            slug = re.sub(r'[^a-z0-9]+', '-', user_prompt.lower().strip())[:40].strip('-')
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            project_name = f"{timestamp}_{slug}"
            project_dir = os.path.join(config.WORKSPACE, project_name)

            config.WORKSPACE = os.path.abspath(project_dir)
            Path(config.WORKSPACE).mkdir(parents=True, exist_ok=True)

        log.info(f"Profile: {self.profile.name()}")
        log.info(f"Project directory: {config.WORKSPACE}")

        # Initialize git before runtime metadata is created so .harness does not
        # become part of the initial project commit.
        git_dir = Path(config.WORKSPACE) / ".git"
        if not git_dir.exists():
            subprocess.run(["git", "init"], cwd=config.WORKSPACE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "add", "-A"], cwd=config.WORKSPACE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["git", "commit", "-m", "init", "--allow-empty"],
                cwd=config.WORKSPACE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        permission_mode = os.environ.get("HARNESS_PERMISSION_MODE", "workspace-write")
        session_store = SessionStore(Path(config.WORKSPACE) / ".harness")
        session = session_store.create(
            profile=self.profile.name(),
            cwd=Path(config.WORKSPACE),
            model=config.MODEL,
            permission_mode=permission_mode,
        )
        event_bus = session_store.event_bus(session)
        tool_context = ToolContext(
            workspace=WorkspaceService(
                root=config.WORKSPACE,
                snapshots_dir=session.snapshots_dir,
            ),
            permission_policy=PermissionPolicy(mode=permission_mode),
            event_bus=event_bus,
            session_id=session.id,
            approval_provider=ConsoleApprovalProvider(),
        )
        setattr(self.main_agent, "tool_context", tool_context)
        event_bus.emit(
            "session_started",
            agent="main_agent",
            payload={"profile": self.profile.name(), "workspace": config.WORKSPACE},
        )

        total_start = time.time()
        from middlewares import TimeBudgetMiddleware
        assert self.main_agent is not None
        for mw in self.main_agent.middlewares:
            if isinstance(mw, TimeBudgetMiddleware):
                mw.sync_start_time(total_start)
                task_timeout = self.profile.resolve_task_timeout(user_prompt)
                if task_timeout:
                    mw.budget_seconds = task_timeout
                    log.info(f"Time budget set to {task_timeout}s from task metadata")

        log.info("=" * 60)
        log.info("MAIN AGENT LOOP")
        log.info("=" * 60)
        try:
            self.main_agent.run(self._format_main_agent_task(user_prompt))
        finally:
            tools.stop_dev_server()
            event_bus.emit(
                "session_finished",
                agent="main_agent",
                payload={"profile": self.profile.name()},
            )

        total_duration = time.time() - total_start
        log.info("=" * 60)
        log.info(f"HARNESS COMPLETE — total time: {total_duration / 60:.1f} minutes")
        log.info(f"Output in: {config.WORKSPACE}")
        log.info("=" * 60)

    def _format_main_agent_task(self, user_prompt: str) -> str:
        criteria = self.profile.acceptance_criteria()
        criteria_text = "\n".join(f"- {item}" for item in criteria) if criteria else "- Verify the task requirements before stopping."
        return (
            f"Task:\n{user_prompt}\n\n"
            f"Acceptance criteria:\n{criteria_text}\n\n"
            "Main-agent ownership rules:\n"
            "- Only the main agent may modify files, create tests, integrate results, and decide when to stop.\n"
            "- Consultation sub-agents are read-only and may only return findings, evidence, recommendations, and risks.\n"
            "- Verify the acceptance criteria against actual files or command output before stopping."
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    from logger import setup_logging
    setup_logging(verbose="--verbose" in sys.argv or "-v" in sys.argv)

    # Parse flags
    args = [a for a in sys.argv[1:] if a not in ("--verbose", "-v")]

    # --list-profiles
    if "--list-profiles" in args:
        print("Available profiles:\n")
        for p in list_profiles():
            print(f"  {p['name']:15s} {p['description']}")
        sys.exit(0)

    # --profile <name>
    profile_name = "app-builder"
    if "--profile" in args:
        idx = args.index("--profile")
        if idx + 1 < len(args):
            profile_name = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("Error: --profile requires a name")
            sys.exit(1)

    if not config.API_KEY:
        print("Error: Set OPENAI_API_KEY in .env or environment.")
        sys.exit(1)

    if len(args) < 1:
        print("Usage: python harness.py [--profile <name>] \"<task>\" [--verbose]")
        print()
        print("Profiles:")
        for p in list_profiles():
            print(f"  {p['name']:15s} {p['description']}")
        print()
        print("Examples:")
        print('  python harness.py "Build a DAW in the browser"')
        print('  python harness.py --profile terminal "Fix the broken symlinks in /tmp"')
        print('  python harness.py --profile swe-bench "Fix the TypeError in parse_config()"')
        print('  python harness.py --profile reasoning "What is the escape velocity of Mars?"')
        sys.exit(1)

    user_prompt = " ".join(args)

    try:
        profile = get_profile(profile_name)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    log.info(f"Prompt: {user_prompt}")
    log.info(f"Profile: {profile_name}")
    log.info(f"Model: {config.MODEL}")
    log.info(f"Base URL: {config.BASE_URL}")
    log.info(f"Workspace: {config.WORKSPACE}")

    # Preflight — verify API connection with retries for rate limits
    # Skip in benchmark mode (HARNESS_FLAT_WORKSPACE) to avoid wasting an API
    # call and hitting rate limits when many containers start simultaneously.
    if os.environ.get("HARNESS_FLAT_WORKSPACE"):
        log.info("Benchmark mode — skipping API preflight check.")
        preflight_ok = True
    else:
        log.info("Verifying API connection...")
        from agents import get_client
        import random
        preflight_ok = False
        for attempt in range(8):
            try:
                resp = get_client().chat.completions.create(
                    model=config.MODEL,
                    messages=[{"role": "user", "content": "Say OK"}],
                    max_tokens=5,
                )
                log.info(f"API OK — model responded: {resp.choices[0].message.content}")
                preflight_ok = True
                break
            except Exception as e:
                err_str = str(e)
                if "rate_limit" in err_str or "429" in err_str:
                    # Exponential backoff with jitter to avoid thundering herd
                    base_wait = min(2 ** (attempt + 1), 60)
                    jitter = random.uniform(0, base_wait * 0.5)
                    wait = base_wait + jitter
                    log.warning(f"API rate limited (attempt {attempt+1}/8), waiting {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    log.error(f"API preflight failed: {e}")
                    break

    if not preflight_ok:
        print(f"\nCannot connect to API. Check your .env:\n"
              f"  OPENAI_API_KEY  — is it valid?\n"
              f"  OPENAI_BASE_URL — is {config.BASE_URL} correct?\n"
              f"  HARNESS_MODEL   — does {config.MODEL} exist on this provider?")
        sys.exit(1)

    harness = Harness(profile)
    try:
        harness.run(user_prompt)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        sys.exit(130)
    except Exception as e:
        log.error(f"Harness crashed with unhandled exception: {e}", exc_info=True)
        # Exit 1 signals failure to Harbor, but at least we log the cause
        sys.exit(1)


if __name__ == "__main__":
    main()
