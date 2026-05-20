from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..agent.loop import AgentConversation
from ..profiles import get_profile, list_profiles
from ..profiles.base import BaseProfile
from ..runtime import tools
from ..runtime.approvals import ConsoleApprovalProvider
from ..runtime.middlewares import TimeBudgetMiddleware
from ..runtime.permissions import PermissionPolicy
from ..runtime.tool_context import ToolContext
from ..sessions.store import SessionStore
from ..skills import SkillRegistry
from ..workspace.service import WorkspaceService
from .mentions import MentionResolutionError, format_turn_with_mentions, resolve_mentions


GIT_COMMIT_AUTHOR = ("Harness", "harness@example.invalid")
PRODUCT_DEFAULT_PROFILE = "coding-agent"
CHECKPOINT_EXCLUDES = [".harness", config.PROGRESS_FILE]
PROFILE_SLASH_ALIASES = {
    "/code": "coding-agent",
    "/app": "app-builder",
    "/terminal": "terminal",
    "/swe": "swe-bench",
    "/plan": "plan",
}


@dataclass
class CheckpointConfig:
    auto: bool = True
    every_turns: int = 1


@dataclass
class TurnResult:
    text: str
    checkpoint: str
    notice: str = ""


@dataclass
class ProfileSwitchEvent:
    previous: str
    current: str
    reason: str


class InteractiveSession:
    def __init__(
        self,
        *,
        cwd: str | Path,
        profile_name: str = PRODUCT_DEFAULT_PROFILE,
        resume_session_id: str | None = None,
    ):
        self.cwd = Path(cwd).resolve()
        config.WORKSPACE = str(self.cwd)
        self.profile = get_profile(profile_name)
        self.skill_registry = SkillRegistry()
        self.session_store = SessionStore(self.cwd / ".harness")
        self.resume_session_id = resume_session_id
        self.resume_context: str | None = None
        self.force_profile_name = profile_name
        if resume_session_id:
            metadata = self.session_store.read_metadata(resume_session_id)
            self.cwd = Path(metadata["cwd"]).resolve()
            config.WORKSPACE = str(self.cwd)
            self.session_store = SessionStore(self.cwd / ".harness")
            self.profile = get_profile(metadata.get("profile") or profile_name)
            self.resume_context = _build_resume_context(self.session_store, resume_session_id)

        _ensure_git_repository(self.cwd)
        self.permission_mode = os.environ.get("HARNESS_PERMISSION_MODE", "workspace-write")
        self.session = self.session_store.create(
            profile=self.profile.name(),
            cwd=self.cwd,
            model=config.MODEL,
            permission_mode=self.permission_mode,
            resumed_from=resume_session_id,
        )
        self.event_bus = self.session_store.event_bus(self.session)
        self.tool_context = ToolContext(
            workspace=WorkspaceService(
                root=self.cwd,
                snapshots_dir=self.session.snapshots_dir,
            ),
            permission_policy=PermissionPolicy(mode=self.permission_mode),
            event_bus=self.event_bus,
            session_id=self.session.id,
            approval_provider=ConsoleApprovalProvider(),
        )
        self.agent = self._build_agent()
        self.conversation: AgentConversation = self.agent.start_conversation()
        self.turn_count = 0
        self.checkpoint = CheckpointConfig()
        self.pending_plan_markdown: str | None = None
        self.last_user_task: str = ""
        self.last_assistant_text: str = ""
        self.profile_history: list[ProfileSwitchEvent] = []
        self._started_at = time.time()
        self._sync_time_budget()
        self.event_bus.emit(
            "session_started",
            agent="main_agent",
            payload={
                "profile": self.profile.name(),
                "workspace": str(self.cwd),
                "resumed_from": resume_session_id,
                "interactive": True,
            },
        )
        if self.resume_context:
            self.conversation.messages.append({
                "role": "user",
                "content": f"Resume context:\n{self.resume_context}",
            })

    def _build_agent(self):
        from ..agent.loop import Agent

        cfg = self.profile.main_agent()
        catalog = self.skill_registry.build_catalog_prompt()
        return Agent(
            "main_agent",
            cfg.system_prompt + catalog,
            use_tools=True,
            extra_tool_schemas=cfg.extra_tool_schemas,
            tool_schemas=cfg.tool_schemas,
            middlewares=cfg.middlewares,
            time_budget=cfg.time_budget,
            tool_context=self.tool_context,
        )

    def _sync_time_budget(self) -> None:
        for mw in self.agent.middlewares:
            if isinstance(mw, TimeBudgetMiddleware):
                mw.sync_start_time(self._started_at)

    def format_task(self, user_prompt: str) -> str:
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

    def submit(self, user_prompt: str) -> TurnResult:
        if self.pending_plan_markdown and self.profile.name() == "plan":
            if _is_plan_execution_confirmation(user_prompt):
                return self.execute_pending_plan()
            return self.revise_pending_plan(user_prompt)
        return self._submit_to_current_agent(user_prompt)

    def _submit_to_current_agent(self, user_prompt: str) -> TurnResult:
        baseline_dirty = git_dirty_paths(self.cwd)
        baseline_staged = git_staged_paths(self.cwd)
        resolved = resolve_mentions(
            user_prompt,
            workspace_root=self.cwd,
            session_store=self.session_store,
        )
        prompt_with_mentions = format_turn_with_mentions(user_prompt, resolved)
        task = self.format_task(prompt_with_mentions)
        self.turn_count += 1
        self.event_bus.emit(
            "turn_started",
            agent="main_agent",
            payload={
                "turn": self.turn_count,
                "mentions": [item.raw for item in resolved],
            },
        )
        text = self.conversation.submit(task)
        self.last_user_task = user_prompt
        self.last_assistant_text = text
        notice = self._capture_plan_handoff(text)
        checkpoint = self._maybe_auto_checkpoint(
            baseline_dirty=baseline_dirty,
            baseline_staged=baseline_staged,
        )
        self.event_bus.emit(
            "turn_finished",
            agent="main_agent",
            payload={
                "turn": self.turn_count,
                "checkpoint": checkpoint,
            },
        )
        return TurnResult(text=text, checkpoint=checkpoint, notice=notice)

    def execute_pending_plan(self) -> TurnResult:
        if not self.pending_plan_markdown:
            raise ValueError("No pending plan to execute. Switch to /plan and create a plan first.")
        plan_markdown = self.pending_plan_markdown
        self.pending_plan_markdown = None
        self._switch_profile(
            PRODUCT_DEFAULT_PROFILE,
            reason="execute approved plan",
            plan_markdown=plan_markdown,
        )
        task = (
            "Execute the approved implementation plan below in coding-agent mode.\n\n"
            "Use the plan as the source of truth, but still inspect the repository, "
            "make the smallest appropriate code/test changes, and run verification before stopping."
        )
        return self._submit_to_current_agent(task)

    def revise_pending_plan(self, feedback: str) -> TurnResult:
        if not self.pending_plan_markdown:
            raise ValueError("No pending plan to revise. Switch to /plan and create a plan first.")
        feedback = feedback.strip()
        if not feedback:
            raise ValueError("Provide feedback for the pending plan, or say 'continue' to execute it.")
        return self._submit_to_current_agent(
            "Revise the previous Markdown plan using this user feedback. "
            "Return the complete updated plan in the required structured Markdown format.\n\n"
            f"User feedback:\n{feedback}"
        )

    def _capture_plan_handoff(self, text: str) -> str:
        if self.profile.name() != "plan" or not text.strip():
            return ""
        self.pending_plan_markdown = text.strip()
        self.event_bus.emit(
            "plan_ready",
            agent="main_agent",
            payload={"profile": self.profile.name()},
        )
        return (
            "Plan ready. Say 'continue' to switch to coding-agent mode and implement it, "
            "or reply with feedback to revise the plan."
        )

    def _switch_profile(
        self,
        profile_name: str,
        *,
        reason: str = "slash command",
        plan_markdown: str | None = None,
    ) -> None:
        previous = self.profile.name()
        if previous == profile_name:
            return
        self.conversation.close()
        self.profile = get_profile(profile_name)
        self.agent = self._build_agent()
        self.conversation = self.agent.start_conversation()
        handoff = self._build_profile_handoff_context(
            previous_profile=previous,
            current_profile=self.profile.name(),
            reason=reason,
            plan_markdown=plan_markdown,
        )
        if handoff:
            self.conversation.messages.append({
                "role": "user",
                "content": handoff,
            })
        self._sync_time_budget()
        self.profile_history.append(ProfileSwitchEvent(
            previous=previous,
            current=self.profile.name(),
            reason=reason,
        ))
        self.event_bus.emit(
            "profile_switched",
            agent="main_agent",
            payload={
                "previous_profile": previous,
                "profile": self.profile.name(),
                "reason": reason,
                "handoff_context": bool(handoff),
                "plan_included": bool(plan_markdown),
            },
        )

    def _build_profile_handoff_context(
        self,
        *,
        previous_profile: str,
        current_profile: str,
        reason: str,
        plan_markdown: str | None,
    ) -> str:
        lines = [
            "Profile handoff context:",
            f"- Workspace: {self.cwd}",
            f"- Session: {self.session.id}",
            f"- Previous profile: {previous_profile}",
            f"- Current profile: {current_profile}",
            f"- Switch reason: {reason}",
        ]
        if self.last_user_task:
            lines.append("")
            lines.append("Most recent user task:")
            lines.append(_truncate_handoff_text(self.last_user_task))
        if self.last_assistant_text and not plan_markdown:
            lines.append("")
            lines.append("Most recent assistant summary:")
            lines.append(_truncate_handoff_text(self.last_assistant_text))
        if plan_markdown:
            lines.append("")
            lines.append("Approved Markdown plan:")
            lines.append(plan_markdown)
        return "\n".join(lines)

    def handle_slash_command(self, line: str) -> bool:
        parts = shlex.split(line)
        if not parts:
            return True
        command = parts[0]
        args = parts[1:]
        try:
            if command in {"/exit", "/quit"}:
                return False
            if command == "/help":
                print_help()
            elif command == "/sessions":
                print_sessions(self.session_store)
            elif command == "/session":
                _require_arg(args, "Usage: /session <session-id>")
                print_session(self.session_store, args[0])
            elif command == "/fork":
                _require_arg(args, "Usage: /fork <session-id>")
                print_fork(self.session_store, args[0])
            elif command == "/resume":
                _require_arg(args, "Usage: /resume <session-id>")
                self._inject_resume_context(args[0])
            elif command == "/rollback":
                if len(args) != 2:
                    raise ValueError("Usage: /rollback <session-id> <path>")
                rollback_session_file(self.session_store, args[0], args[1])
            elif command == "/profiles":
                print_profiles()
            elif command in PROFILE_SLASH_ALIASES:
                if args:
                    raise ValueError(f"Usage: {command}")
                print(self.switch_profile(PROFILE_SLASH_ALIASES[command]))
            elif command == "/doctor":
                run_doctor(self.cwd)
            elif command == "/config" and args == ["show"]:
                print_config_show(self.cwd)
            elif command == "/checkpoint":
                self._handle_checkpoint_command(args)
            else:
                print(f"Unknown slash command: {command}")
        except (FileNotFoundError, ValueError, KeyError, MentionResolutionError) as e:
            print(f"Error: {e}")
        return True

    def switch_profile(self, profile_name: str) -> str:
        previous = self.profile.name()
        self.pending_plan_markdown = None
        self._switch_profile(profile_name)
        current = self.profile.name()
        if current == previous:
            return f"profile already active: {current}"
        return f"profile switched: {previous} -> {current}"

    def _inject_resume_context(self, session_id: str) -> None:
        context_text = _build_resume_context(self.session_store, session_id)
        self.conversation.messages.append({
            "role": "user",
            "content": f"Resume context:\n{context_text}",
        })
        print(f"Resumed context injected for session: {session_id}")

    def _handle_checkpoint_command(self, args: list[str]) -> None:
        if not args:
            print(self.create_checkpoint(manual=True))
            return
        if args[:2] == ["auto", "on"]:
            self.checkpoint.auto = True
            print("checkpoint auto: on")
            return
        if args[:2] == ["auto", "off"]:
            self.checkpoint.auto = False
            print("checkpoint auto: off")
            return
        if args[:2] == ["every", "turn"]:
            self.checkpoint.every_turns = 1
            print("checkpoint cadence: every turn")
            return
        if len(args) == 2 and args[0] == "every":
            try:
                turns = int(args[1])
            except ValueError as e:
                raise ValueError("Usage: /checkpoint every <N> turns") from e
            if turns < 1:
                raise ValueError("Checkpoint cadence must be at least 1 turn")
            self.checkpoint.every_turns = turns
            print(f"checkpoint cadence: every {turns} turns")
            return
        if len(args) == 3 and args[0] == "every" and args[2] == "turns":
            try:
                turns = int(args[1])
            except ValueError as e:
                raise ValueError("Usage: /checkpoint every <N> turns") from e
            if turns < 1:
                raise ValueError("Checkpoint cadence must be at least 1 turn")
            self.checkpoint.every_turns = turns
            print(f"checkpoint cadence: every {turns} turns")
            return
        if args == ["status"]:
            print(
                f"checkpoint auto: {'on' if self.checkpoint.auto else 'off'}; "
                f"cadence: every {self.checkpoint.every_turns} turn(s)"
            )
            return
        raise ValueError("Usage: /checkpoint [auto on|auto off|every turn|every <N> turns|status]")

    def _maybe_auto_checkpoint(
        self,
        *,
        baseline_dirty: set[str],
        baseline_staged: set[str],
    ) -> str:
        if not self.checkpoint.auto:
            return "checkpoint auto off"
        if self.turn_count % self.checkpoint.every_turns != 0:
            return "checkpoint cadence skipped"
        if baseline_staged:
            return "checkpoint skipped: staged changes existed before turn"
        return self.create_checkpoint(manual=False, baseline_dirty=baseline_dirty)

    def create_checkpoint(
        self,
        *,
        manual: bool,
        baseline_dirty: set[str] | None = None,
    ) -> str:
        if not git_has_committable_changes(self.cwd):
            return "no changes to checkpoint"
        paths_to_add = None
        if not manual and baseline_dirty is not None:
            current_dirty = git_dirty_paths(self.cwd)
            paths_to_add = sorted(current_dirty - baseline_dirty)
            if not paths_to_add:
                return "no changes to checkpoint"
        if paths_to_add is None:
            git_add_runtime_excluded(self.cwd)
        else:
            git_add_paths(self.cwd, paths_to_add)
        if not git_has_staged_changes(self.cwd):
            return "no changes to checkpoint"
        detail = "manual" if manual else f"turn {self.turn_count}"
        message = f"checkpoint: {self.session.id} {detail}"
        subprocess.run(
            git_commit_command(message),
            cwd=self.cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return f"checkpoint created: {rev}"

    def close(self) -> None:
        tools.stop_dev_server()
        self.conversation.close()
        self.event_bus.emit(
            "session_finished",
            agent="main_agent",
            payload={"profile": self.profile.name(), "interactive": True},
        )


def _require_arg(args: list[str], usage: str) -> None:
    if len(args) != 1:
        raise ValueError(usage)


def _is_plan_execution_confirmation(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    return normalized in {
        "continue",
        "go ahead",
        "proceed",
        "execute",
        "implement",
        "start",
        "yes",
        "ok",
        "继续",
        "执行",
        "开始",
        "实施",
        "可以",
        "好",
        "好的",
        "按计划执行",
        "继续执行",
    }



def _truncate_handoff_text(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def print_turn_result(result: TurnResult) -> None:
    if result.text:
        print(result.text)
    if result.notice:
        print(result.notice)
    if result.checkpoint:
        print(result.checkpoint)


def _ensure_git_repository(workspace: Path) -> None:
    if (workspace / ".git").exists():
        _git_add_runtime_exclude(workspace)
        return
    subprocess.run(["git", "init"], cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    _git_add_runtime_exclude(workspace)
    subprocess.run(git_commit_command("init", allow_empty=True), cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def _git_add_runtime_exclude(workspace: Path) -> None:
    info_exclude = workspace / ".git" / "info" / "exclude"
    info_exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = info_exclude.read_text(encoding="utf-8", errors="replace") if info_exclude.exists() else ""
    lines = set(existing.splitlines())
    additions = [item for item in [".harness/", config.PROGRESS_FILE] if item not in lines]
    if additions:
        with info_exclude.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for item in additions:
                f.write(item + "\n")


def git_add_runtime_excluded(workspace: Path) -> None:
    subprocess.run(
        ["git", "add", "-A", "--", "."],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["git", "reset", "-q", "--", ".harness", config.PROGRESS_FILE],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def git_add_paths(workspace: Path, paths: list[str]) -> None:
    subprocess.run(
        ["git", "add", "--", *paths],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def git_has_committable_changes(workspace: Path) -> bool:
    result = subprocess.run(
        runtime_excluded_git_command("status", "--porcelain"),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def git_dirty_paths(workspace: Path) -> set[str]:
    result = subprocess.run(
        runtime_excluded_git_command("status", "--porcelain"),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            paths.add(path)
    return paths


def git_staged_paths(workspace: Path) -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def git_has_staged_changes(workspace: Path) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 1


def runtime_excluded_git_command(*args: str) -> list[str]:
    command = ["git", *args, "--", "."]
    command.extend(f":(exclude){item}" for item in CHECKPOINT_EXCLUDES)
    return command


def git_commit_command(message: str, *, allow_empty: bool = False) -> list[str]:
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


def print_help() -> None:
    print("hca commands:")
    print("  /help")
    print("  /sessions")
    print("  /session <session-id>")
    print("  /resume <session-id>")
    print("  /fork <session-id>")
    print("  /rollback <session-id> <path>")
    print("  /profiles")
    print("  /code | /plan | /terminal | /swe | /app")
    print("  /doctor")
    print("  /config show")
    print("  /checkpoint [auto on|auto off|every turn|every <N> turns|status]")
    print("  /exit")


def print_sessions(store: SessionStore) -> None:
    sessions = store.list_sessions()
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


def print_session(store: SessionStore, session_id: str) -> None:
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
            print(f"- {_event_summary(event)}")


def print_fork(store: SessionStore, session_id: str) -> None:
    session = store.fork(session_id)
    metadata = store.read_metadata(session.id)
    print(f"forked_session: {session.id}")
    print(f"forked_from: {metadata.get('forked_from', session_id)}")
    print(f"profile: {metadata.get('profile', '')}")
    print(f"cwd: {metadata.get('cwd', '')}")


def rollback_session_file(store: SessionStore, session_id: str, path: str) -> None:
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


def print_profiles() -> None:
    print("Available profiles:\n")
    for profile in list_profiles():
        print(f"  {profile['name']:15s} {profile['description']}")


def print_config_show(workspace: Path) -> None:
    print("Harness config")
    print(f"api_key: {_redact_secret(config.API_KEY)}")
    print(f"base_url: {config.BASE_URL}")
    print(f"model: {config.MODEL}")
    print(f"workspace: {workspace}")
    print(f"permission_mode: {os.environ.get('HARNESS_PERMISSION_MODE', 'workspace-write')}")
    print(f"checkpoint_auto: interactive default")
    print(f"compress_threshold: {config.COMPRESS_THRESHOLD}")
    print(f"reset_threshold: {config.RESET_THRESHOLD}")
    print(f"max_agent_iterations: {config.MAX_AGENT_ITERATIONS}")


def run_doctor(workspace: Path) -> int:
    failures = 0
    print("Harness doctor")
    failures += _doctor_line("API key", bool(config.API_KEY), "configured" if config.API_KEY else "missing OPENAI_API_KEY")
    failures += _doctor_line("API base URL", bool(config.BASE_URL), config.BASE_URL or "missing OPENAI_BASE_URL")
    failures += _doctor_line("Workspace", workspace.exists() and workspace.is_dir(), str(workspace))
    failures += _doctor_line("Git", shutil_which("git") is not None, shutil_which("git") or "not installed")
    failures += _doctor_line("Shell", shell_path() is not None, shell_path() or "no shell found")
    return 0 if failures == 0 else 1


def _doctor_line(label: str, ok: bool, detail: str) -> int:
    print(f"{'OK' if ok else 'FAIL':4s} {label:18s} {detail}")
    return 0 if ok else 1


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def shell_path() -> str | None:
    return shutil_which("pwsh") or shutil_which("powershell") or os.environ.get("ComSpec")


def _redact_secret(value: str) -> str:
    if not value:
        return "unset"
    if len(value) <= 8:
        return "set"
    return f"{value[:4]}...{value[-4:]}"


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
    return f"#{event.get('sequence')} {event.get('type')} agent={event.get('agent')}{suffix}"
