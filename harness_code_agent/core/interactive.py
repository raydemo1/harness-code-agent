from __future__ import annotations

import os
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import config
from ..agent.loop import AgentConversation
from ..agent.prompts import GlobalRulesDoc, PromptPrefixBuilder
from ..profiles import get_profile, list_profiles
from ..profiles.base import BaseProfile
from ..runtime import tools
from ..runtime.approvals import ApprovalProvider, ConsoleApprovalProvider
from ..runtime.middlewares import StaticVerifierMiddleware, TimeBudgetMiddleware
from ..runtime.mcp import McpClientManager, McpConfigError, load_mcp_config
from ..runtime.permission_middleware import PermissionMiddleware
from ..runtime.permissions import PermissionPolicy
from ..runtime.questions import ConsoleQuestionProvider, QuestionProvider
from ..runtime.tool_context import ToolContext
from ..sessions.events import AssistantMessageEvent, FinalReportEvent, SessionFinishedEvent, UserInputEvent
from ..sessions.report import build_final_report
from ..sessions.summary import load_session_summary
from ..sessions.store import SessionStore
from ..skills import SkillRegistry
from ..workspace.service import WorkspaceService
from ..workspace.shell_session import docker_cli_path, docker_shell_hint, sandbox_mode, windows_shell_hint, windows_shell_path
from .mentions import MentionResolutionError, format_turn_with_mentions, resolve_mentions


GIT_COMMIT_AUTHOR = ("Harness", "harness@example.invalid")
PRODUCT_DEFAULT_PROFILE = "coding-agent"
CHECKPOINT_EXCLUDES = [".harness", "global_plan", config.PROGRESS_FILE]
PROFILE_SLASH_ALIASES = {
    "/code": "coding-agent",
    "/app": "app-builder",
    "/terminal": "terminal",
    "/swe": "swe-bench",
    "/plan": "plan",
}
log = logging.getLogger("harness")


@dataclass
class CheckpointConfig:
    auto: bool = True
    every_turns: int = 1


@dataclass
class TurnResult:
    text: str
    checkpoint: str
    notice: str = ""
    streamed: bool = False


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
        stream_sink: Callable[[str], None] | None = None,
        event_listener: Callable[[object], None] | None = None,
        approval_provider: ApprovalProvider | None = None,
        question_provider: QuestionProvider | None = None,
        output_sink: Callable[[str], None] | None = None,
        stream_callback=None,
    ):
        self.cwd = Path(cwd).resolve()
        config.WORKSPACE = str(self.cwd)
        self.stream_sink = stream_sink or stream_callback
        self.event_listener = event_listener
        self.approval_provider = approval_provider or ConsoleApprovalProvider()
        self.question_provider = question_provider or ConsoleQuestionProvider()
        self.output_sink = output_sink or print
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
        self.event_bus = self.session_store.event_bus(self.session, listener=self.event_listener)
        self._load_mcp_tools()
        self.tool_context = ToolContext(
            workspace=WorkspaceService(
                root=self.cwd,
                snapshots_dir=self.session.snapshots_dir,
            ),
            permission_policy=PermissionPolicy(mode=self.permission_mode),
            event_bus=self.event_bus,
            session_id=self.session.id,
            approval_provider=self.approval_provider,
            question_provider=self.question_provider,
            tool_registry=self.tool_registry,
        )
        self.agent = self._build_agent()
        self.conversation: AgentConversation = self.agent.start_conversation()
        self.turn_count = 0
        self.checkpoint = CheckpointConfig()
        # Wire compaction manager
        from ..agent.compaction import CompactionManager
        self.conversation.compaction_mgr = CompactionManager(
            compacted_dir=self.session.compacted_dir,
        )
        self.conversation._event_bus = self.event_bus
        self.pending_plan_markdown: str | None = None
        self.pending_plan_revision = 0
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
            self._append_conversation_message({
                "role": "user",
                "content": f"Resume context:\n{self.resume_context}",
            })

    def _build_agent(self):
        from ..agent.loop import Agent

        cfg = self.profile.main_agent()
        harness_rules = _load_harness_rules(self.cwd)
        catalog = self.skill_registry.build_catalog_prompt()
        acceptance_criteria = (
            self.profile.acceptance_criteria()
            if hasattr(self.profile, "acceptance_criteria")
            else []
        )
        prefix = PromptPrefixBuilder().build(
            profile_prompt=cfg.system_prompt,
            global_rules_docs=[harness_rules] if harness_rules is not None else [],
            skill_catalog=catalog,
            acceptance_criteria=acceptance_criteria,
        )
        middlewares = list(cfg.middlewares)
        middlewares.append(
            PermissionMiddleware(
                tool_context=self.tool_context,
                tool_registry=self.tool_registry,
            )
        )
        middlewares.append(
            StaticVerifierMiddleware(workspace_root=str(self.cwd), workspace=self.tool_context.workspace)
        )
        return Agent(
            "main_agent",
            prefix.content,
            use_tools=True,
            tool_schemas=self._tool_schemas_for_agent_config(cfg),
            middlewares=middlewares,
            time_budget=cfg.time_budget,
            tool_context=self.tool_context,
            stream_callback=self.stream_sink,
            prompt_cache_identity=prefix.cache_identity,
        )

    def _load_mcp_tools(self) -> None:
        self.tool_registry = tools.BUILTIN_TOOL_REGISTRY.copy()
        self.mcp_manager = McpClientManager.from_workspace(self.cwd)
        self.mcp_manager.connect_all()
        self.mcp_manager.register_tools(self.tool_registry)

    def _tool_schemas_for_agent_config(self, cfg) -> list[dict]:
        return tools.tool_schemas_for_profile(
            allowed_permissions=cfg.allowed_tool_permissions,
            include_names=cfg.allowed_tool_names,
            exclude_names=cfg.blocked_tool_names,
            registry=self.tool_registry,
        )

    def _refresh_agent_tool_schemas(self) -> None:
        cfg = self.profile.main_agent()
        schemas = self._tool_schemas_for_agent_config(cfg)
        self.agent.update_tool_schemas(schemas)

    def _sync_time_budget(self) -> None:
        for mw in self.agent.middlewares:
            if isinstance(mw, TimeBudgetMiddleware):
                mw.sync_start_time(self._started_at)

    def format_task(self, user_prompt: str) -> str:
        return f"Task:\n{user_prompt}"

    def submit(self, user_prompt: str, cancellation_token=None) -> TurnResult:
        if self.pending_plan_markdown and self.profile.name() == "plan":
            if _is_plan_execution_confirmation(user_prompt):
                return self.execute_pending_plan()
            return self.revise_pending_plan(user_prompt)
        return self._submit_to_current_agent(user_prompt, cancellation_token=cancellation_token)

    def _submit_to_current_agent(self, user_prompt: str, cancellation_token=None) -> TurnResult:
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
        self.event_bus.emit_event(
            UserInputEvent(
                text=user_prompt,
                turn=self.turn_count,
                mentions=[item.raw for item in resolved],
            ).to_event()
        )
        self.event_bus.emit(
            "turn_started",
            agent="main_agent",
            payload={
                "turn": self.turn_count,
                "mentions": [item.raw for item in resolved],
            },
        )
        text = self.conversation.submit(task, cancellation_token=cancellation_token)
        if cancellation_token is not None and cancellation_token.is_cancelled:
            from ..agent.cancellation import CancelledError
            raise CancelledError("Turn cancelled by user")
        streamed = bool(getattr(self.conversation, "last_run_streamed_text", False))
        self.event_bus.emit_event(
            AssistantMessageEvent(
                text=text,
                turn=self.turn_count,
            ).to_event()
        )
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
        return TurnResult(text=text, checkpoint=checkpoint, notice=notice, streamed=streamed)

    def interrupt_current_shell(self) -> bool:
        """Best-effort interrupt for a shell command owned by the active conversation."""
        runtime_state = getattr(self.conversation, "runtime_state", None)
        shell_session = getattr(runtime_state, "shell_session", None)
        if shell_session is None:
            return False
        try:
            shell_session.interrupt()
        except Exception as exc:
            log.debug("Failed to interrupt active shell session: %s", exc)
            return False
        return True

    def execute_pending_plan(self) -> TurnResult:
        if not self.pending_plan_markdown:
            raise ValueError("No pending plan to execute. Switch to /plan and create a plan first.")
        plan_markdown = self.pending_plan_markdown
        self.pending_plan_markdown = None
        self.pending_plan_revision = 0
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
        self.pending_plan_revision += 1
        plan_path = self._write_pending_plan_artifact(self.pending_plan_markdown)
        self.event_bus.emit(
            "plan_ready",
            agent="main_agent",
            payload={
                "profile": self.profile.name(),
                "plan_path": str(plan_path.relative_to(self.cwd)),
                "plan_revision": self.pending_plan_revision,
                "approval_source": "/plan",
            },
        )
        return (
            "计划已写入 `global_plan/current/plan.md`。"
            "在 TUI 中选择 `执行计划` 继续，或在 `修改计划` 输入框中输入修改理由。"
            "非 TUI 入口可回复 continue/继续 执行，其他文本会作为修改理由。"
        )

    def _write_pending_plan_artifact(self, plan_markdown: str) -> Path:
        plan_path = self.cwd / "global_plan" / "current" / "plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan_markdown.rstrip() + "\n", encoding="utf-8")
        return plan_path

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
        from ..agent.compaction import CompactionManager
        self.conversation.compaction_mgr = CompactionManager(
            compacted_dir=self.session.compacted_dir,
        )
        self.conversation._event_bus = self.event_bus
        handoff = self._build_profile_handoff_context(
            previous_profile=previous,
            current_profile=self.profile.name(),
            reason=reason,
            plan_markdown=plan_markdown,
        )
        if handoff:
            self._append_conversation_message({
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
        from ..tui.commands import default_command_registry

        result = default_command_registry().execute(line, self)
        if result.text:
            self.output_sink(result.text)
        return result.should_continue

    def switch_profile(self, profile_name: str) -> str:
        previous = self.profile.name()
        self.pending_plan_markdown = None
        self.pending_plan_revision = 0
        self._switch_profile(profile_name)
        current = self.profile.name()
        if current == previous:
            return f"profile already active: {current}"
        return f"profile switched: {previous} -> {current}"

    def set_permission_mode(self, permission_mode: str) -> str:
        PermissionPolicy(mode=permission_mode)
        previous = self.permission_mode
        if previous == permission_mode:
            return f"permission mode already active: {permission_mode}"
        self.permission_mode = permission_mode
        self.tool_context.permission_policy = PermissionPolicy(mode=permission_mode)
        self.session_store.update_permission_mode(self.session.id, permission_mode)
        self.event_bus.emit(
            "permission_mode_switched",
            agent="main_agent",
            payload={
                "previous_permission_mode": previous,
                "permission_mode": permission_mode,
            },
        )
        return f"permission mode switched: {previous} -> {permission_mode}"

    def toggle_permission_mode(self) -> str:
        next_mode = (
            PermissionPolicy.DANGER_FULL_ACCESS
            if self.permission_mode == PermissionPolicy.WORKSPACE_WRITE
            else PermissionPolicy.WORKSPACE_WRITE
        )
        return self.set_permission_mode(next_mode)

    def mcp_status(self) -> str:
        return self.mcp_manager.status_report()

    def mcp_list(self) -> str:
        return self.mcp_manager.tools_report()

    def reload_mcp(self) -> str:
        self.mcp_manager.close()
        self._load_mcp_tools()
        self.tool_context.tool_registry = self.tool_registry
        self._refresh_agent_tool_schemas()
        self.event_bus.emit(
            "mcp_reloaded",
            agent="main_agent",
            payload={"tool_count": len(getattr(self.mcp_manager, "tool_bindings", []))},
        )
        return "MCP reloaded\n" + self.mcp_manager.status_report()

    def _inject_resume_context(self, session_id: str) -> str:
        context_text = _build_resume_context(self.session_store, session_id)
        self._append_conversation_message({
            "role": "user",
            "content": f"Resume context:\n{context_text}",
        })
        return f"Resumed context injected for session: {session_id}"

    def manual_compact_context(self) -> str:
        from ..agent import context
        from ..agent.compaction import get_thresholds
        from ..agent.loop import llm_call_simple

        conv = self.conversation
        token_count = context.count_tokens(conv.messages)
        msg_count_before = len(conv.messages)
        mgr = getattr(conv, "compaction_mgr", None)
        if mgr is not None:
            split_index = context.choose_compaction_split_index(
                conv.messages,
                force=True,
                target_tokens=get_thresholds().allow,
            )
            system_len = 1 if conv.messages and conv.messages[0].get("role") == "system" else 0
            if split_index <= system_len:
                return "Compaction skipped: not enough old context to summarize."
            candidate = mgr.generate_candidate(
                conv.messages,
                llm_call=llm_call_simple,
                split_index=split_index,
                revision=conv.compaction_gate.revision,
            )
            commit = mgr.commit_candidate_to_messages(
                candidate,
                conv.messages,
                current_revision=conv.compaction_gate.revision,
            )
            if not commit.committed or commit.messages is None:
                return f"Compaction skipped: {commit.reason or 'unable to commit summary'}"
            conv.messages = commit.messages
        else:
            conv.messages = context.compact_messages(
                conv.messages,
                llm_call_simple,
                role=conv.agent.name,
                force=True,
                target_tokens=get_thresholds().allow,
            )
        msg_count_after = len(conv.messages)
        conv.runtime_state.current_turn_start_index = max(1, len(conv.messages) - 1)
        conv.compaction_gate.bump_revision()
        conv.compaction_gate.mark_compacted()
        tokens_saved = max(0, token_count - context.count_tokens(conv.messages))
        return f"Compacted: {msg_count_before} -> {msg_count_after} messages, ~{tokens_saved} tokens saved."

    def _append_conversation_message(self, message: dict) -> None:
        append = getattr(self.conversation, "_append_message", None)
        if append is not None:
            append(message)
        else:
            self.conversation.messages.append(message)

    def _handle_checkpoint_command(self, args: list[str]) -> str:
        if not args:
            return self.create_checkpoint(manual=True)
        if args[:2] == ["auto", "on"]:
            self.checkpoint.auto = True
            return "checkpoint auto: on"
        if args[:2] == ["auto", "off"]:
            self.checkpoint.auto = False
            return "checkpoint auto: off"
        if args[:2] == ["every", "turn"]:
            self.checkpoint.every_turns = 1
            return "checkpoint cadence: every turn"
        if args and args[0] == "every":
            if len(args) in (2, 3) and (len(args) == 2 or args[2] in ("turn", "turns")):
                try:
                    turns = int(args[1])
                except ValueError as e:
                    raise ValueError("Usage: /checkpoint every <N> turns") from e
                if turns < 1:
                    raise ValueError("Checkpoint cadence must be at least 1 turn")
                self.checkpoint.every_turns = turns
                return f"checkpoint cadence: every {turns} turns"

        if args == ["status"]:
            return (
                f"checkpoint auto: {'on' if self.checkpoint.auto else 'off'}; "
                f"cadence: every {self.checkpoint.every_turns} turn(s)"
            )
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
        self.mcp_manager.close()
        try:
            metadata = self.session_store.read_metadata(self.session.id)
            events = self.session_store.read_events(self.session.id)
            self.event_bus.emit_event(
                FinalReportEvent(
                    **build_final_report(
                        metadata,
                        events,
                        status="closed",
                        reason="user_exit",
                        summary=self.last_assistant_text,
                    )
                ).to_event()
            )
        except Exception as exc:
            log.warning("Failed to write final report for session %s: %s", self.session.id, exc)
        self.event_bus.emit_event(
            SessionFinishedEvent(
                reason="user_exit",
                status="closed",
            ).to_event()
        )
        self.session_store.update_status(self.session.id, "closed")
        try:
            self.session_store.write_summary(self.session.id)
        except Exception as exc:
            log.warning("Failed to write summary for session %s: %s", self.session.id, exc)


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


def _load_harness_rules(workspace: Path) -> GlobalRulesDoc | None:
    path = workspace / "HARNESS.md"
    if not path.exists() or not path.is_file():
        return None
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        return None
    return GlobalRulesDoc(source=str(path), content=content)



def _truncate_handoff_text(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def print_turn_result(result: TurnResult) -> None:
    if result.streamed:
        print()
    elif result.text:
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
    additions = [item for item in [".harness/", "global_plan/", config.PROGRESS_FILE] if item not in lines]
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
        ["git", "reset", "-q", "--", ".harness", "global_plan", config.PROGRESS_FILE],
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
    from ..tui.commands import default_command_registry

    print(default_command_registry().format_help())


def format_sessions(store: SessionStore) -> str:
    sessions = store.list_sessions()
    if not sessions:
        return "No sessions found."
    lines = [f"{'ID':28s} {'PROFILE':15s} {'MODE':18s} CREATED"]
    for item in sessions:
        lines.append(
            f"{item.get('id', ''):28s} "
            f"{item.get('profile', ''):15s} "
            f"{item.get('permission_mode', ''):18s} "
            f"{item.get('created_at', '')}"
        )
    return "\n".join(lines)


def print_sessions(store: SessionStore) -> None:
    print(format_sessions(store))


def print_session(store: SessionStore, session_id: str) -> None:
    print(load_session_summary(store, session_id))


def format_fork(store: SessionStore, session_id: str) -> str:
    session = store.fork(session_id)
    metadata = store.read_metadata(session.id)
    return "\n".join([
        f"forked_session: {session.id}",
        f"forked_from: {metadata.get('forked_from', session_id)}",
        f"profile: {metadata.get('profile', '')}",
        f"cwd: {metadata.get('cwd', '')}",
    ])


def print_fork(store: SessionStore, session_id: str) -> None:
    print(format_fork(store, session_id))


def format_rollback_session_file(store: SessionStore, session_id: str, path: str) -> str:
    metadata = store.read_metadata(session_id)
    workspace = WorkspaceService(
        root=metadata["cwd"],
        snapshots_dir=store.sessions_dir / session_id / "snapshots",
    )
    result = workspace.rollback_latest_snapshot(path)
    lines = [
        f"rolled_back: {path}",
        f"workspace: {workspace.root}",
    ]
    if result.snapshot_path:
        lines.append(f"pre_rollback_snapshot: {result.snapshot_path}")
    return "\n".join(lines)


def rollback_session_file(store: SessionStore, session_id: str, path: str) -> None:
    print(format_rollback_session_file(store, session_id, path))


def format_profiles() -> str:
    lines = ["Available profiles:", ""]
    for profile in list_profiles():
        lines.append(f"  {profile['name']:15s} {profile['description']}")
    return "\n".join(lines)


def print_profiles() -> None:
    print(format_profiles())


def format_config_show(workspace: Path) -> str:
    lines = [
        "Harness config",
        f"api_key: {_redact_secret(config.API_KEY)}",
        f"base_url: {config.BASE_URL}",
        f"model: {config.MODEL}",
        f"workspace: {workspace}",
        f"permission_mode: {os.environ.get('HARNESS_PERMISSION_MODE', 'workspace-write')}",
        f"sandbox_mode: {config.SANDBOX_MODE}",
        f"docker_image: {config.DOCKER_IMAGE}",
        f"docker_network: {config.DOCKER_NETWORK}",
        f"provider: {config.PROVIDER}",
        f"stream: {config.STREAM}",
    ]
    if os.name == "nt":
        lines.append(f"windows_shell: {config.WINDOWS_SHELL} ({windows_shell_hint()})")
    lines.extend([
        "checkpoint_auto: interactive default",
        f"compress_threshold: {config.COMPRESS_THRESHOLD}",
        f"reset_threshold: {config.RESET_THRESHOLD}",
        f"max_agent_iterations: {config.MAX_AGENT_ITERATIONS}",
        f"max_agent_total_tokens: {config.MAX_AGENT_TOTAL_TOKENS}",
        f"max_agent_tool_calls: {config.MAX_AGENT_TOOL_CALLS}",
        f"agent_budget_warn_fraction: {config.AGENT_BUDGET_WARN_FRACTION}",
    ])
    return "\n".join(lines)


def print_config_show(workspace: Path) -> None:
    print(format_config_show(workspace))


def format_doctor(workspace: Path, *, mcp_manager: McpClientManager | None = None) -> tuple[str, int]:
    rows = []
    rows.append(("API key", bool(config.API_KEY), "configured" if config.API_KEY else "missing OPENAI_API_KEY"))
    rows.append(("API base URL", bool(config.BASE_URL), config.BASE_URL or "missing OPENAI_BASE_URL"))
    rows.append(("Workspace", workspace.exists() and workspace.is_dir(), str(workspace)))
    rows.append(("Git", shutil_which("git") is not None, shutil_which("git") or "not installed"))
    if sandbox_mode() == "docker":
        docker = docker_cli_path()
        rows.append(("Docker", docker is not None, docker_shell_hint()))
    else:
        shell = shell_path()
        rows.append(("Shell", shell is not None, shell or "no shell found"))
    rows.append(("MCP", *_mcp_doctor_status(workspace, mcp_manager=mcp_manager)))
    failures = sum(0 if ok else 1 for _, ok, _ in rows)
    lines = ["Harness doctor"]
    lines.extend(_format_doctor_line(label, ok, detail) for label, ok, detail in rows)
    return "\n".join(lines), failures


def run_doctor(workspace: Path) -> int:
    text, failures = format_doctor(workspace)
    print(text)
    return 0 if failures == 0 else 1


def _format_doctor_line(label: str, ok: bool, detail: str) -> str:
    return f"{'OK' if ok else 'FAIL':4s} {label:18s} {detail}"


def _mcp_doctor_status(workspace: Path, *, mcp_manager: McpClientManager | None = None) -> tuple[bool, str]:
    if mcp_manager is not None:
        return mcp_manager.doctor_status()
    try:
        cfg = load_mcp_config(workspace)
    except McpConfigError as exc:
        return False, str(exc)
    if not cfg.path.exists():
        return True, "not configured"
    return True, f"{len(cfg.servers)} configured server(s)"


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def shell_path() -> str | None:
    if sandbox_mode() == "docker":
        return docker_cli_path()
    if os.name == "nt":
        return windows_shell_path()
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
