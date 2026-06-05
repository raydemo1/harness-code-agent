from __future__ import annotations

import os
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import config
from ..agent.conversation import AgentConversation
from ..agent.prompts import GlobalRulesDoc, PromptPrefixBuilder
from ..profiles import get_profile, list_profiles
from ..profiles.base import BaseProfile
from ..profiles.router import RouteDecision, route_profile_for_task
from ..runtime.builtins.browser import stop_dev_server
from ..runtime.builtins.registry import BUILTIN_TOOL_REGISTRY
from ..runtime.approvals import ApprovalProvider, ConsoleApprovalProvider
from ..runtime.middleware import StaticVerifierMiddleware, TimeBudgetMiddleware
from ..runtime.mcp import McpClientManager, McpConfigError, load_mcp_config
from ..runtime.permission_middleware import PermissionMiddleware
from ..runtime.permissions import PermissionPolicy
from ..runtime.questions import ConsoleQuestionProvider, QuestionProvider
from ..runtime.tool_context import ToolContext
from ..runtime.tool_registry import tool_schemas_for_profile
from ..sessions.events import AssistantMessageEvent, FinalReportEvent, SessionFinishedEvent, TurnSummaryEvent, UserInputEvent
from ..sessions.report import build_final_report
from ..sessions._event_helpers import is_ignored_changed_file
from ..sessions.summary import load_session_summary
from ..sessions.store import Session, SessionStore
from ..sessions.turn_summary import generate_turn_summary, should_summarize_turn
from ..skills import SkillRegistry
from ..workspace.service import WorkspaceService
from ..workspace.shell_session import docker_cli_path, docker_info_check, docker_shell_hint, sandbox_mode, windows_shell_hint, windows_shell_path
from .mentions import MentionResolutionError, ResolvedMention, render_mention_context, resolve_mentions


GIT_COMMIT_AUTHOR = ("Harness", "harness@example.invalid")
PRODUCT_DEFAULT_PROFILE = "coding-agent"
CHECKPOINT_EXCLUDES = [".harness", "global_plan", config.PROGRESS_FILE]
PROFILE_SLASH_ALIASES = {
    "/code": "coding-agent",
    "/app": "app-builder",
    "/terminal": "terminal",
    "/swe": "swe-bench",
    "/plan": "plan",
    "/review": "review",
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


@dataclass
class ProfileSlot:
    profile: BaseProfile
    agent: object
    conversation: AgentConversation


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
        profile_explicit: bool | None = None,
    ):
        self.cwd = Path(cwd).resolve()
        config.WORKSPACE = str(self.cwd)
        self.stream_sink = stream_sink or stream_callback
        self.event_listener = event_listener
        self.approval_provider = approval_provider or ConsoleApprovalProvider()
        self.question_provider = question_provider or ConsoleQuestionProvider()
        self.output_sink = output_sink or print
        self.skill_registry = SkillRegistry()
        self.session_store = SessionStore(self.cwd / ".harness")
        self.session_store.root.mkdir(parents=True, exist_ok=True)
        self.resume_session_id = resume_session_id
        self.resume_context: str | None = None
        inferred_explicit = profile_name != PRODUCT_DEFAULT_PROFILE
        self.profile_explicit = inferred_explicit if profile_explicit is None else profile_explicit
        self._pending_profile_name = profile_name
        self._profile_source = "explicit" if self.profile_explicit else "default"
        if resume_session_id:
            metadata = self.session_store.read_metadata(resume_session_id)
            self.cwd = Path(metadata["cwd"]).resolve()
            config.WORKSPACE = str(self.cwd)
            self.session_store = SessionStore(self.cwd / ".harness")
            self.session_store.root.mkdir(parents=True, exist_ok=True)
            self._pending_profile_name = metadata.get("profile") or profile_name
            self._profile_source = "resume"
            self.resume_context = _build_resume_context(self.session_store, resume_session_id)

        self.profile = get_profile(self._pending_profile_name)
        _ensure_git_repository(self.cwd)
        self.permission_mode = os.environ.get("HARNESS_PERMISSION_MODE", "workspace-write")
        self.session: Session | None = None
        self.event_bus = None
        self.tool_context: ToolContext | None = None
        self.tool_registry = None
        self.mcp_manager = None
        self.agent = None
        self.conversation: AgentConversation | None = None
        self.profile_slots: dict[str, ProfileSlot] = {}
        self._active_profile_name: str | None = None
        self.turn_count = 0
        self.checkpoint = CheckpointConfig()
        self.pending_plan_markdown: str | None = None
        self.pending_plan_revision = 0
        self.last_user_task: str = ""
        self.last_assistant_text: str = ""
        self.profile_history: list[ProfileSwitchEvent] = []
        self._started_at: float | None = None
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def is_bound(self) -> bool:
        return self.session is not None and self.conversation is not None

    @property
    def session_id(self) -> str | None:
        return self.session.id if self.session is not None else None

    @property
    def display_profile(self) -> str:
        if self.is_bound or self._profile_source in {"explicit", "resume"}:
            return self.profile.name()
        return "pending"

    def _build_agent(self, profile: BaseProfile):
        from ..agent.conversation import Agent

        if self.tool_context is None:
            raise RuntimeError("Cannot build agent before the session is bound.")
        cfg = profile.main_agent()
        self.tool_context.allowed_tool_permissions = set(cfg.allowed_tool_permissions)
        self.tool_context.blocked_tool_names = set(cfg.blocked_tool_names)
        harness_rules = _load_harness_rules(self.cwd)
        catalog = self.skill_registry.build_catalog_prompt()
        acceptance_criteria = (
            profile.acceptance_criteria()
            if hasattr(profile, "acceptance_criteria")
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
        self.tool_registry = BUILTIN_TOOL_REGISTRY.copy()
        self.mcp_manager = McpClientManager.from_workspace(self.cwd)
        self.mcp_manager.connect_all()
        self.mcp_manager.register_tools(self.tool_registry)

    def _ensure_mcp_tools_loaded(self) -> None:
        if self.mcp_manager is None or self.tool_registry is None:
            self._load_mcp_tools()

    def _tool_schemas_for_agent_config(self, cfg) -> list[dict]:
        core_schemas = tool_schemas_for_profile(
            allowed_permissions=cfg.allowed_tool_permissions,
            include_names=cfg.allowed_tool_names,
            exclude_names=cfg.blocked_tool_names,
            registry=self.tool_registry,
        )
        revealed = set()
        if self.tool_context is not None:
            self.tool_context.allowed_tool_permissions = set(cfg.allowed_tool_permissions)
            self.tool_context.blocked_tool_names = set(cfg.blocked_tool_names)
            revealed = set(self.tool_context.revealed_tool_names)
        if not revealed:
            return core_schemas
        revealed_schemas = tool_schemas_for_profile(
            allowed_permissions=cfg.allowed_tool_permissions,
            include_names=revealed,
            exclude_names=cfg.blocked_tool_names,
            registry=self.tool_registry,
            disclosure={"deferred"},
        )
        known = {
            schema.get("function", {}).get("name")
            for schema in core_schemas
            if isinstance(schema, dict)
        }
        return core_schemas + [
            schema
            for schema in revealed_schemas
            if schema.get("function", {}).get("name") not in known
        ]

    def _refresh_agent_tool_schemas(self) -> None:
        if not self.profile_slots:
            return
        for slot in self.profile_slots.values():
            cfg = slot.profile.main_agent()
            schemas = self._tool_schemas_for_agent_config(cfg)
            slot.agent.update_tool_schemas(schemas)

    def _sync_time_budget(self) -> None:
        if self.agent is None:
            return
        for mw in self.agent.middlewares:
            if isinstance(mw, TimeBudgetMiddleware):
                mw.sync_start_time(self._started_at)

    def format_task(self, user_prompt: str) -> str:
        return f"Task:\n{user_prompt}"

    def submit(self, user_prompt: str, cancellation_token=None) -> TurnResult:
        self.ensure_profile_bound_for_first_task(user_prompt)
        if self.pending_plan_markdown and self.profile.name() == "plan":
            if _is_plan_execution_confirmation(user_prompt):
                return self.execute_pending_plan()
            return self.revise_pending_plan(user_prompt)
        return self._submit_to_current_agent(user_prompt, cancellation_token=cancellation_token)

    def ensure_profile_bound_for_first_task(self, user_prompt: str) -> None:
        if self.is_bound:
            return
        route_decision: RouteDecision | None = None
        profile_name = self._pending_profile_name
        source = self._profile_source
        if source == "default":
            route_decision = route_profile_for_task(user_prompt, workspace=self.cwd)
            profile_name = route_decision.profile_name
            source = "router" if not route_decision.fallback_used else "default"
        self._bind_profile(profile_name, source=source, route_decision=route_decision)

    def _bind_profile(
        self,
        profile_name: str,
        *,
        source: str,
        route_decision: RouteDecision | None = None,
    ) -> None:
        if self.is_bound:
            return
        self.profile = get_profile(profile_name)
        self._pending_profile_name = self.profile.name()
        self._profile_source = source
        self.session = self.session_store.create(
            profile=self.profile.name(),
            cwd=self.cwd,
            model=config.MODEL,
            permission_mode=self.permission_mode,
            resumed_from=self.resume_session_id,
            profile_source=source,
        )
        self.event_bus = self.session_store.event_bus(self.session, listener=self.event_listener)
        self._ensure_mcp_tools_loaded()
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
        self._started_at = time.time()
        self._activate_profile_slot(self.profile.name(), create_handoff=False)
        self.event_bus.emit(
            "session_started",
            agent="main_agent",
            payload={
                "session_id": self.session.id,
                "profile": self.profile.name(),
                "profile_source": source,
                "workspace": str(self.cwd),
                "resumed_from": self.resume_session_id,
                "interactive": True,
            },
        )
        if route_decision is not None:
            self.event_bus.emit(
                "profile_route_decision",
                agent="main_agent",
                payload={
                    "profile": route_decision.profile_name,
                    "confidence": route_decision.confidence,
                    "reason": route_decision.reason,
                    "fallback_used": route_decision.fallback_used,
                    "fallback_reason": route_decision.fallback_reason,
                },
            )
        if self.resume_context:
            self._append_conversation_message({
                "role": "user",
                "content": f"Resume context:\n{self.resume_context}",
            })

    def _activate_profile_slot(
        self,
        profile_name: str,
        *,
        create_handoff: bool,
        reason: str = "slash command",
        previous_profile: str | None = None,
        plan_markdown: str | None = None,
    ) -> bool:
        if self.session is None or self.event_bus is None or self.tool_context is None:
            raise RuntimeError("Cannot activate a profile slot before the session is bound.")
        created = False
        slot = self.profile_slots.get(profile_name)
        if slot is None:
            profile = get_profile(profile_name)
            agent = self._build_agent(profile)
            conversation = agent.start_conversation()
            conversation._event_bus = self.event_bus
            slot = ProfileSlot(profile=profile, agent=agent, conversation=conversation)
            self.profile_slots[profile.name()] = slot
            created = True
        self._active_profile_name = slot.profile.name()
        self.profile = slot.profile
        self.agent = slot.agent
        self.conversation = slot.conversation
        if created:
            self._inject_memory_navigation()
        if create_handoff and (created or plan_markdown):
            handoff = self._build_profile_handoff_context(
                previous_profile=previous_profile or "",
                current_profile=self.profile.name(),
                reason=reason,
                plan_markdown=plan_markdown,
            )
            if handoff:
                self._append_conversation_message({"role": "user", "content": handoff})
        self._sync_time_budget()
        return created

    def _submit_to_current_agent(self, user_prompt: str, cancellation_token=None) -> TurnResult:
        turn_started_at = time.time()
        baseline_dirty = git_dirty_paths(self.cwd)
        baseline_staged = git_staged_paths(self.cwd)
        resolved = resolve_mentions(
            user_prompt,
            workspace_root=self.cwd,
            session_store=self.session_store,
            skill_catalog=self.skill_registry.catalog,
        )
        memory_block = self._memory_recall_block(
            user_prompt,
            mention_paths=_memory_mention_paths(resolved),
        )
        prompt_with_mentions = _format_turn_with_mentions_and_memory(user_prompt, resolved, memory_block)
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
        turn_event_start = len(getattr(self.event_bus, "events", []))
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
        self._maybe_emit_turn_summary(
            user_prompt=user_prompt,
            assistant_text=text,
            checkpoint=checkpoint,
            turn_event_start=turn_event_start,
            duration_seconds=time.time() - turn_started_at,
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

    def _maybe_emit_turn_summary(
        self,
        *,
        user_prompt: str,
        assistant_text: str,
        checkpoint: str,
        turn_event_start: int,
        duration_seconds: float,
    ) -> None:
        if self.event_bus is None or self.event_listener is None:
            return
        events = [
            event.to_dict() if hasattr(event, "to_dict") else dict(event)
            for event in getattr(self.event_bus, "events", [])[turn_event_start:]
        ]
        if not should_summarize_turn(
            events,
            profile_name=self.profile.name(),
            duration_seconds=duration_seconds,
        ):
            return
        summary = generate_turn_summary(
            events,
            user_prompt=user_prompt,
            assistant_text=assistant_text,
            checkpoint=checkpoint,
        )
        self.event_bus.emit_event(
            TurnSummaryEvent(
                turn=self.turn_count,
                summary=summary.summary,
                duration_seconds=duration_seconds,
                tool_counts=summary.tool_counts,
                changed_files=summary.changed_files,
                checkpoint=checkpoint,
                generated_by=summary.generated_by,
            ).to_event()
        )

    def interrupt_current_shell(self) -> bool:
        """Best-effort interrupt for a shell command owned by the active conversation."""
        if self.conversation is None:
            return False
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
        if not self.is_bound:
            self.profile = get_profile(profile_name)
            self._pending_profile_name = self.profile.name()
            self._profile_source = "explicit"
            return
        target_existed = profile_name in self.profile_slots
        self._activate_profile_slot(
            profile_name,
            create_handoff=True,
            reason=reason,
            previous_profile=previous,
            plan_markdown=plan_markdown,
        )
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
                "handoff_context": (not target_existed) or bool(plan_markdown),
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
            f"- Session: {self.session.id if self.session is not None else '<pending>'}",
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
        if not self.is_bound:
            return f"profile selected: {current}"
        return f"profile switched: {previous} -> {current}"

    def set_permission_mode(self, permission_mode: str) -> str:
        PermissionPolicy(mode=permission_mode)
        previous = self.permission_mode
        if previous == permission_mode:
            return f"permission mode already active: {permission_mode}"
        self.permission_mode = permission_mode
        if self.tool_context is not None:
            self.tool_context.permission_policy = PermissionPolicy(mode=permission_mode)
        if self.session is not None:
            self.session_store.update_permission_mode(self.session.id, permission_mode)
        if self.event_bus is not None:
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
        self._ensure_mcp_tools_loaded()
        return self.mcp_manager.status_report()

    def mcp_list(self) -> str:
        self._ensure_mcp_tools_loaded()
        return self.mcp_manager.tools_report()

    def reload_mcp(self) -> str:
        self._ensure_mcp_tools_loaded()
        self.mcp_manager.close()
        self._load_mcp_tools()
        if self.tool_context is not None:
            self.tool_context.tool_registry = self.tool_registry
        self._refresh_agent_tool_schemas()
        if self.event_bus is not None:
            self.event_bus.emit(
                "mcp_reloaded",
                agent="main_agent",
                payload={"tool_count": len(getattr(self.mcp_manager, "tool_bindings", []))},
            )
        return "MCP reloaded\n" + self.mcp_manager.status_report()

    def _inject_resume_context(self, session_id: str) -> str:
        context_text = _build_resume_context(self.session_store, session_id)
        self.resume_session_id = session_id
        self.resume_context = context_text
        if not self.is_bound:
            return f"Resume context queued for session: {session_id}"
        self._append_conversation_message({
            "role": "user",
            "content": f"Resume context:\n{context_text}",
        })
        return f"Resumed context injected for session: {session_id}"

    def manual_compact_context(self) -> str:
        if not self.is_bound or self.conversation is None:
            return "No active session yet. Submit a task first."
        from ..agent import context
        from ..agent.compaction import get_thresholds
        from ..agent.conversation import llm_call_simple

        conv = self.conversation
        token_count = context.count_tokens(conv.messages)
        msg_count_before = len(conv.messages)
        compacted = context.compact_messages(
            conv.messages,
            llm_call_simple,
            role=conv.agent.name,
            force=True,
            target_tokens=get_thresholds().summary_target,
        )
        replace = getattr(conv, "_replace_messages", None)
        if replace is not None:
            replace(compacted)
        else:
            conv.messages = compacted
            conv.compaction_gate.bump_revision()
        msg_count_after = len(conv.messages)
        conv.runtime_state.current_turn_start_index = max(1, len(conv.messages) - 1)
        conv.compaction_gate.mark_compacted()
        tokens_saved = max(0, token_count - context.count_tokens(conv.messages))
        return f"Compacted: {msg_count_before} -> {msg_count_after} messages, ~{tokens_saved} tokens saved."

    def _append_conversation_message(self, message: dict) -> None:
        append = getattr(self.conversation, "_append_message", None)
        if append is not None:
            append(message)
        else:
            self.conversation.messages.append(message)

    def _memory_store(self):
        from ..memory.store import MemoryStore, default_memory_root

        return MemoryStore(default_memory_root(self.cwd), workspace=self.cwd)

    def _maybe_run_memory_dream(self) -> None:
        if _memory_disabled():
            return
        from ..memory.dream import run_dream, should_dream

        store = self._memory_store()
        try:
            if should_dream(store):
                run_dream(store)
        except Exception as exc:
            log.debug("Memory Dream skipped after error: %s", exc)

    def _inject_memory_navigation(self) -> None:
        if _memory_disabled() or self.conversation is None:
            return
        self._maybe_run_memory_dream()
        store = self._memory_store()
        if not store.exists() or not store.has_active_records():
            return
        content = store.read_memory_file("MEMORY.md").strip()
        if not content:
            return
        self._append_conversation_message(
            {
                "role": "user",
                "content": (
                    "Long-term memory navigation (dynamic user-context, not stable prompt prefix):\n"
                    f"{content}"
                ),
            }
        )

    def _memory_recall_block(self, user_prompt: str, *, mention_paths: list[str]) -> str:
        if _memory_disabled():
            return ""
        self._maybe_run_memory_dream()
        store = self._memory_store()
        if not store.exists() or not store.has_active_records():
            return ""
        try:
            from ..memory.recall import MemoryRecall

            recall = MemoryRecall(store)
            hits = recall.search(
                user_prompt,
                mentions=mention_paths,
            )
            return recall.format_block(hits)
        except Exception as exc:
            log.debug("Memory recall skipped after error: %s", exc)
            return ""

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
        if self.session is None:
            return "checkpoint skipped: no active session"
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
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._maybe_run_memory_dream()
        stop_dev_server()
        for slot in list(self.profile_slots.values()):
            slot.conversation.close()
        if self.mcp_manager is not None:
            self.mcp_manager.close()
        if self.session is None or self.event_bus is None:
            return
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


def _memory_mention_paths(resolved: list[ResolvedMention]) -> list[str]:
    paths: list[str] = []
    for item in resolved:
        if item.kind not in {"file", "directory"}:
            continue
        value = item.metadata.get("path") or item.resolved or item.target
        if value:
            paths.append(str(value))
    return paths


def _format_turn_with_mentions_and_memory(
    user_text: str,
    resolved: list[ResolvedMention],
    memory_block: str,
) -> str:
    parts = []
    mention_context = render_mention_context(resolved)
    if mention_context:
        parts.append(mention_context)
    if memory_block:
        parts.append(memory_block)
    if not parts:
        return user_text
    parts.append(f"User turn:\n{user_text}")
    return "\n\n".join(parts)


def _memory_disabled() -> bool:
    return os.environ.get("HARNESS_MEMORY_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


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


from .formatters import (
    _build_resume_context,
    _event_summary,
    format_config_show,
    format_doctor,
    format_fork,
    format_profiles,
    format_rollback_session_file,
    format_sessions,
    print_config_show,
    print_fork,
    print_help,
    print_profiles,
    print_session,
    print_sessions,
    rollback_session_file,
    run_doctor,
)
from .git_helpers import (
    _ensure_git_repository,
    git_add_paths,
    git_add_runtime_excluded,
    git_commit_command,
    git_dirty_paths,
    git_has_committable_changes,
    git_has_staged_changes,
    git_staged_paths,
    runtime_excluded_git_command,
)
