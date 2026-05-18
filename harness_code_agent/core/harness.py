#!/usr/bin/env python3
"""
Harness — profile-driven main-agent architecture for autonomous task execution.

The core loop is owned by one main agent. Consultation sub-agents are read-only
helpers for local investigation, parallel search, test design, and review.

Built-in profiles:
  coding-agent — Work in a local repository with sessions, permissions, and verification
  app-builder  — Build web apps from a prompt (original Anthropic article scenario)
  terminal     — Solve terminal/CLI tasks (Terminal-Bench-2 style)
  swe-bench    — Fix GitHub issues in real repos
  reasoning    — Knowledge-intensive QA (MMMU-Pro style)

Usage:
  python harness.py run "Fix the failing tests"                    # default: coding-agent
  python harness.py run --profile terminal "Fix the broken git merge"
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
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import config
from ..agent.loop import Agent
from ..profiles import get_profile, list_profiles
from ..profiles.base import BaseProfile
from ..runtime import tools
from ..runtime.approvals import ConsoleApprovalProvider
from ..runtime.permissions import PermissionPolicy
from ..runtime.tool_context import ToolContext
from ..sessions.store import SessionStore
from ..skills import SkillRegistry
from ..workspace.service import WorkspaceService

log = logging.getLogger("harness")

COMMIT_POLICIES = {"none", "checkpoint", "milestone"}
GIT_COMMIT_AUTHOR = ("Harness", "harness@example.invalid")
LEGACY_DEFAULT_PROFILE = "app-builder"
PRODUCT_DEFAULT_PROFILE = "coding-agent"
SLASH_COMMANDS = {
    "/sessions": "sessions",
    "/session": "session",
    "/fork": "fork",
    "/resume": "resume",
    "/rollback": "rollback",
    "/doctor": "doctor",
    "/profiles": "--list-profiles",
}


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

    def run(
        self,
        user_prompt: str,
        *,
        resume_context: str | None = None,
        force_flat_workspace: bool = False,
        resume_source_id: str | None = None,
    ) -> None:
        # Create a unique project subdirectory under workspace
        # (skip if HARNESS_FLAT_WORKSPACE is set — used for benchmarks
        #  where outputs must land directly in the workspace root)
        if force_flat_workspace or os.environ.get("HARNESS_FLAT_WORKSPACE"):
            Path(config.WORKSPACE).mkdir(parents=True, exist_ok=True)
        else:
            from datetime import datetime
            slug = re.sub(r'[^a-z0-9]+', '-', user_prompt.lower().strip())[:40].strip('-')
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            project_name = f"{timestamp}_{slug}"
            project_dir = os.path.join(config.WORKSPACE, project_name)

            config.WORKSPACE = os.path.abspath(project_dir)
            Path(config.WORKSPACE).mkdir(parents=True, exist_ok=True)

        commit_policy = _resolve_commit_policy()

        log.info(f"Profile: {self.profile.name()}")
        log.info(f"Project directory: {config.WORKSPACE}")

        # Initialize git before runtime metadata is created so .harness does not
        # become part of the initial project commit.
        _ensure_git_repository(Path(config.WORKSPACE))

        permission_mode = os.environ.get("HARNESS_PERMISSION_MODE", "workspace-write")
        session_store = SessionStore(Path(config.WORKSPACE) / ".harness")
        session = session_store.create(
            profile=self.profile.name(),
            cwd=Path(config.WORKSPACE),
            model=config.MODEL,
            permission_mode=permission_mode,
            resumed_from=resume_source_id,
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
        self.main_agent.tool_context = tool_context
        event_bus.emit(
            "session_started",
            agent="main_agent",
            payload={
                "profile": self.profile.name(),
                "workspace": config.WORKSPACE,
                "resumed_from": resume_source_id,
            },
        )

        total_start = time.time()
        from ..runtime.middlewares import TimeBudgetMiddleware
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
        run_failed = False
        try:
            self.main_agent.run(
                self._format_main_agent_task(
                    user_prompt,
                    resume_context=resume_context,
                )
            )
        except Exception:
            run_failed = True
            raise
        finally:
            tools.stop_dev_server()
            _commit_session_changes(
                Path(config.WORKSPACE),
                policy=commit_policy,
                user_prompt=user_prompt,
                session_id=session.id,
                run_failed=run_failed,
            )
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

    def _format_main_agent_task(
        self,
        user_prompt: str,
        *,
        resume_context: str | None = None,
    ) -> str:
        criteria = self.profile.acceptance_criteria()
        criteria_text = "\n".join(f"- {item}" for item in criteria) if criteria else "- Verify the task requirements before stopping."
        resume_text = (
            f"Resume context:\n{resume_context}\n\n"
            if resume_context
            else ""
        )
        return (
            resume_text +
            f"Task:\n{user_prompt}\n\n"
            f"Acceptance criteria:\n{criteria_text}\n\n"
            "Main-agent ownership rules:\n"
            "- Only the main agent may modify files, create tests, integrate results, and decide when to stop.\n"
            "- Consultation sub-agents are read-only and may only return findings, evidence, recommendations, and risks.\n"
            "- Verify the acceptance criteria against actual files or command output before stopping."
        )


def _resolve_commit_policy() -> str:
    policy = os.environ.get("HARNESS_COMMIT_POLICY", "checkpoint").strip().lower()
    if policy not in COMMIT_POLICIES:
        raise ValueError(
            "HARNESS_COMMIT_POLICY must be one of: "
            + ", ".join(sorted(COMMIT_POLICIES))
        )
    return policy


def _ensure_git_repository(workspace: Path) -> None:
    git_dir = workspace / ".git"
    if git_dir.exists():
        return

    subprocess.run(
        ["git", "init"],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    _git_add_runtime_exclude(workspace)
    subprocess.run(
        _git_commit_command("init", allow_empty=True),
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _commit_session_changes(
    workspace: Path,
    *,
    policy: str,
    user_prompt: str,
    session_id: str,
    run_failed: bool,
) -> None:
    if policy == "none":
        return
    if policy == "milestone" and run_failed:
        return
    if not _git_has_committable_changes(workspace):
        return

    _git_add_runtime_exclude(workspace)
    if not _git_has_staged_changes(workspace):
        return

    prefix = "checkpoint" if policy == "checkpoint" else "milestone"
    detail = session_id if policy == "checkpoint" else _slugify(user_prompt, limit=48)
    message = f"{prefix}: {detail}"
    subprocess.run(
        _git_commit_command(message),
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _git_add_runtime_exclude(workspace: Path) -> None:
    subprocess.run(
        _runtime_excluded_git_command("add", "-A"),
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _git_has_committable_changes(workspace: Path) -> bool:
    result = subprocess.run(
        _runtime_excluded_git_command("status", "--porcelain"),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def _git_has_staged_changes(workspace: Path) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 1


def _runtime_excluded_git_command(*args: str) -> list[str]:
    return [
        "git",
        *args,
        "--",
        ".",
        ":(exclude).harness",
        f":(exclude){config.PROGRESS_FILE}",
    ]


def _git_commit_command(message: str, *, allow_empty: bool = False) -> list[str]:
    name, email = GIT_COMMIT_AUTHOR
    command = [
        "git",
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-m",
        message,
    ]
    if allow_empty:
        command.append("--allow-empty")
    return command


def _slugify(text: str, *, limit: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")
    return (slug or "session")[:limit].strip("-") or "session"


def _session_store() -> SessionStore:
    return SessionStore(Path(config.WORKSPACE) / ".harness")


def _print_sessions() -> None:
    sessions = _session_store().list_sessions()
    if not sessions:
        print("No sessions found.")
        return
    print(f"{'ID':28s} {'PROFILE':15s} {'MODE':18s} CREATED")
    for item in sessions:
        print(
            f"{item.get('id', ''):28s} "
            f"{item.get('profile', ''):15s} "
            f"{item.get('permission_mode', ''):18s} "
            f"{item.get('created_at', '')}"
        )


def _print_session(session_id: str) -> None:
    store = _session_store()
    metadata = store.read_metadata(session_id)
    events = store.read_events(session_id)
    print(f"id: {metadata.get('id', session_id)}")
    if metadata.get("forked_from"):
        print(f"forked_from: {metadata.get('forked_from')}")
    if metadata.get("resumed_from"):
        print(f"resumed_from: {metadata.get('resumed_from')}")
    print(f"profile: {metadata.get('profile', '')}")
    print(f"model: {metadata.get('model', '')}")
    print(f"permission_mode: {metadata.get('permission_mode', '')}")
    print(f"status: {metadata.get('status', '')}")
    print(f"cwd: {metadata.get('cwd', '')}")
    print(f"created_at: {metadata.get('created_at', '')}")
    print(f"events: {len(events)}")
    if events:
        print("recent_events:")
        for event in events[-5:]:
            print(
                f"- #{event.get('sequence')} "
                f"{event.get('type')} "
                f"agent={event.get('agent')}"
            )


def _fork_session(session_id: str) -> None:
    store = _session_store()
    session = store.fork(session_id)
    metadata = store.read_metadata(session.id)
    print(f"forked_session: {session.id}")
    print(f"forked_from: {metadata.get('forked_from', session_id)}")
    print(f"profile: {metadata.get('profile', '')}")
    print(f"cwd: {metadata.get('cwd', '')}")


def _rollback_session_file(session_id: str, path: str) -> None:
    store = _session_store()
    metadata = store.read_metadata(session_id)
    workspace = WorkspaceService(
        root=metadata["cwd"],
        snapshots_dir=store.sessions_dir / session_id / "snapshots",
    )
    result = workspace.rollback_latest_snapshot(path)
    print(f"rolled_back: {path}")
    print(f"workspace: {workspace.root}")
    if result.snapshot_path:
        print(f"pre_rollback_snapshot: {result.snapshot_path}")


def _event_summary(event: dict) -> str:
    payload = event.get("payload") or {}
    payload_bits = []
    for key in sorted(payload)[:4]:
        value = payload[key]
        text = str(value).replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        payload_bits.append(f"{key}={text}")
    suffix = f" ({', '.join(payload_bits)})" if payload_bits else ""
    return (
        f"#{event.get('sequence')} {event.get('type')} "
        f"agent={event.get('agent')}{suffix}"
    )


def _build_resume_context(
    store: SessionStore,
    session_id: str,
    *,
    max_recent_events: int = 8,
) -> str:
    lineage = store.read_lineage(session_id)
    current = lineage[-1]
    lines = [
        f"Resuming session: {current.get('id', session_id)}",
        "Lineage: " + " -> ".join(item.get("id", "") for item in lineage),
        f"Workspace: {current.get('cwd', '')}",
        f"Profile: {current.get('profile', '')}",
        f"Permission mode: {current.get('permission_mode', '')}",
    ]
    if current.get("forked_from"):
        lines.append(f"Forked from: {current.get('forked_from')}")
    lines.append("")
    lines.append("Recent session events:")
    for metadata in lineage:
        events = store.read_events(metadata["id"])
        if not events:
            lines.append(f"- {metadata['id']}: no events")
            continue
        lines.append(f"- {metadata['id']}:")
        for event in events[-max_recent_events:]:
            lines.append(f"  - {_event_summary(event)}")
    return "\n".join(lines)


def _resume_session(session_id: str, follow_up_task: str) -> None:
    store = _session_store()
    metadata = store.read_metadata(session_id)
    resume_context = _build_resume_context(store, session_id)
    config.WORKSPACE = str(Path(metadata["cwd"]).resolve())
    profile = get_profile(metadata.get("profile") or PRODUCT_DEFAULT_PROFILE)
    prompt = follow_up_task.strip() or (
        "Continue from the resumed session. Inspect the repository state, "
        "use the resume context as historical evidence, and proceed with the next useful step."
    )
    Harness(profile).run(
        prompt,
        resume_context=resume_context,
        force_flat_workspace=True,
        resume_source_id=session_id,
    )


def _redact_secret(value: str) -> str:
    if not value:
        return "unset"
    if len(value) <= 8:
        return "set"
    return f"{value[:4]}...{value[-4:]}"


def _print_config_show() -> None:
    print("Harness config")
    print(f"api_key: {_redact_secret(config.API_KEY)}")
    print(f"base_url: {config.BASE_URL}")
    print(f"model: {config.MODEL}")
    print(f"workspace: {config.WORKSPACE}")
    print(f"permission_mode: {os.environ.get('HARNESS_PERMISSION_MODE', 'workspace-write')}")
    print(f"commit_policy: {os.environ.get('HARNESS_COMMIT_POLICY', 'checkpoint')}")
    print(f"compress_threshold: {config.COMPRESS_THRESHOLD}")
    print(f"reset_threshold: {config.RESET_THRESHOLD}")
    print(f"max_harness_rounds: {config.MAX_HARNESS_ROUNDS}")
    print(f"max_agent_iterations: {config.MAX_AGENT_ITERATIONS}")


def _check_command(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"exit {result.returncode}"
    return result.returncode == 0, detail


def _doctor_line(status: str, label: str, detail: str) -> None:
    print(f"{status:4s} {label:18s} {detail}")


def _run_doctor() -> int:
    print("Harness doctor")
    failures = 0

    api_ok = bool(config.API_KEY)
    _doctor_line("OK" if api_ok else "FAIL", "API key", "configured" if api_ok else "missing OPENAI_API_KEY")
    failures += 0 if api_ok else 1

    base_url_ok = bool(config.BASE_URL)
    _doctor_line("OK" if base_url_ok else "FAIL", "API base URL", config.BASE_URL or "missing OPENAI_BASE_URL")
    failures += 0 if base_url_ok else 1

    python_detail = f"{sys.executable} ({sys.version.split()[0]})"
    _doctor_line("OK", "Python", python_detail)

    shell_path = shutil.which("pwsh") or shutil.which("powershell") or os.environ.get("ComSpec", "")
    shell_ok = bool(shell_path)
    _doctor_line("OK" if shell_ok else "FAIL", "Shell", shell_path or "no shell found")
    failures += 0 if shell_ok else 1

    git_ok, git_detail = _check_command(["git", "--version"])
    _doctor_line("OK" if git_ok else "FAIL", "Git", git_detail)
    failures += 0 if git_ok else 1

    workspace = Path(config.WORKSPACE)
    workspace_ok = workspace.exists() and workspace.is_dir()
    _doctor_line("OK" if workspace_ok else "FAIL", "Workspace", str(workspace))
    failures += 0 if workspace_ok else 1

    try:
        PermissionPolicy(mode=os.environ.get("HARNESS_PERMISSION_MODE", "workspace-write"))
        permission_ok = True
        permission_detail = os.environ.get("HARNESS_PERMISSION_MODE", "workspace-write")
    except ValueError as e:
        permission_ok = False
        permission_detail = str(e)
    _doctor_line("OK" if permission_ok else "FAIL", "Permission mode", permission_detail)
    failures += 0 if permission_ok else 1

    try:
        commit_policy = _resolve_commit_policy()
        commit_ok = True
        commit_detail = commit_policy
    except ValueError as e:
        commit_ok = False
        commit_detail = str(e)
    _doctor_line("OK" if commit_ok else "FAIL", "Commit policy", commit_detail)
    failures += 0 if commit_ok else 1

    playwright = shutil.which("playwright")
    _doctor_line("OK" if playwright else "WARN", "Playwright", playwright or "not installed")

    return 0 if failures == 0 else 1


def _handle_product_command(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "sessions":
        _print_sessions()
        sys.exit(0)
    if args[0] == "session":
        if len(args) != 2:
            print("Usage: python harness.py session <session-id>")
            sys.exit(1)
        try:
            _print_session(args[1])
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)
        sys.exit(0)
    if args[0] == "fork":
        if len(args) != 2:
            print("Usage: python harness.py fork <session-id>")
            sys.exit(1)
        try:
            _fork_session(args[1])
        except (FileNotFoundError, ValueError, KeyError) as e:
            print(f"Error: {e}")
            sys.exit(1)
        sys.exit(0)
    if args[0] == "rollback":
        if len(args) != 3:
            print("Usage: python harness.py rollback <session-id> <path>")
            sys.exit(1)
        try:
            _rollback_session_file(args[1], args[2])
        except (FileNotFoundError, ValueError, KeyError) as e:
            print(f"Error: {e}")
            sys.exit(1)
        sys.exit(0)
    if args[:2] == ["config", "show"]:
        _print_config_show()
        sys.exit(0)
    if args[0] == "doctor":
        sys.exit(_run_doctor())
    return False


def _normalize_slash_command(args: list[str]) -> list[str]:
    if not args or not args[0].startswith("/"):
        return args
    command = args[0]
    if command == "/config":
        return ["config", *args[1:]]
    mapped = SLASH_COMMANDS.get(command)
    if mapped is None:
        raise ValueError(f"Unknown slash command: {command}")
    return [mapped, *args[1:]]


def _parse_profile_and_task(args: list[str]) -> tuple[str, list[str]]:
    profile_name = PRODUCT_DEFAULT_PROFILE if args and args[0] == "run" else LEGACY_DEFAULT_PROFILE
    if args and args[0] == "run":
        args = args[1:]

    if "--profile" in args:
        idx = args.index("--profile")
        if idx + 1 < len(args):
            profile_name = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            raise ValueError("--profile requires a name")

    return profile_name, args


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    from .logging_config import setup_logging
    setup_logging(verbose="--verbose" in sys.argv or "-v" in sys.argv)

    # Parse flags
    args = [a for a in sys.argv[1:] if a not in ("--verbose", "-v")]
    try:
        args = _normalize_slash_command(args)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not (args and args[0] in {"run", "resume"}):
        _handle_product_command(args)

    # --list-profiles
    if "--list-profiles" in args:
        print("Available profiles:\n")
        for p in list_profiles():
            print(f"  {p['name']:15s} {p['description']}")
        sys.exit(0)

    resume_session_id = None
    resume_context = None
    force_flat_workspace = False

    if args and args[0] == "resume":
        if len(args) < 2:
            print("Usage: python harness.py resume <session-id> [follow-up task]")
            sys.exit(1)
        resume_session_id = args[1]
        try:
            store = _session_store()
            metadata = store.read_metadata(resume_session_id)
            resume_context = _build_resume_context(store, resume_session_id)
        except (FileNotFoundError, ValueError, KeyError) as e:
            print(f"Error: {e}")
            sys.exit(1)
        config.WORKSPACE = str(Path(metadata["cwd"]).resolve())
        profile_name = metadata.get("profile") or PRODUCT_DEFAULT_PROFILE
        args = args[2:] or [
            "Continue from the resumed session. Inspect the repository state and proceed with the next useful step."
        ]
        force_flat_workspace = True
    else:
        try:
            profile_name, args = _parse_profile_and_task(args)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

    if not config.API_KEY:
        print("Error: Set OPENAI_API_KEY in .env or environment.")
        sys.exit(1)

    if len(args) < 1:
        print("Usage: python harness.py \"<task>\" [--verbose]")
        print("       python harness.py run [--profile <name>] \"<task>\" [--verbose]")
        print("       python harness.py /<command> [args]")
        print()
        print("Slash commands:")
        print("  /sessions")
        print("  /session <session-id>")
        print("  /fork <session-id>")
        print("  /resume <session-id> [follow-up task]")
        print("  /rollback <session-id> <path>")
        print("  /config show")
        print("  /doctor")
        print("  /profiles")
        print()
        print("Profiles:")
        for p in list_profiles():
            print(f"  {p['name']:15s} {p['description']}")
        print()
        print("Examples:")
        print('  python harness.py "Fix the failing tests"')
        print('  python harness.py /sessions')
        print('  python harness.py /resume 20260518-120000-abcd1234 "continue verification"')
        print('  python harness.py run "Fix the failing tests"')
        print('  python harness.py run --profile terminal "Fix the broken symlinks in /tmp"')
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
        from ..agent.loop import get_client
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
        harness.run(
            user_prompt,
            resume_context=resume_context,
            force_flat_workspace=force_flat_workspace,
            resume_source_id=resume_session_id,
        )
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        sys.exit(130)
    except Exception as e:
        log.error(f"Harness crashed with unhandled exception: {e}", exc_info=True)
        # Exit 1 signals failure to Harbor, but at least we log the cause
        sys.exit(1)


if __name__ == "__main__":
    main()
