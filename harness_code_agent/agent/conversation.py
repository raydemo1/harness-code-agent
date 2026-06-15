"""Agent and conversation loop implementation."""
from __future__ import annotations

import logging
import json
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from . import context
from .cancellation import CancelledError
from .compaction import CompactionGate, compaction_action, get_thresholds
from .observations import FactTracker, ObservationStore
from .providers import ProviderAdapter, current_adapter, get_client
from .runtime_state import AgentFallbackState, AgentRuntimeState, TaskBoard
from .tool_executor import ToolExecutor
from .trace import TraceWriter
from .utils import (
    _prompt_cache_key,
    _short_hash,
    _usage_to_dict,
    capture_prompt_cache_shape,
    compare_prompt_cache_shapes,
)
from ..runtime.arg_preview import safe_args_preview
from ..runtime.builtins.registry import TOOL_SCHEMAS
from ..runtime.tool_context import ToolContext
from ..runtime.tool_result import ToolResult
from ..runtime.tool_runner import finalize_intercepted_tool_result


log = logging.getLogger("harness")
DYNAMIC_CONTEXT_MARKER = "[HARNESS_DYNAMIC_CONTEXT:"


def llm_call_simple(messages: list[dict]) -> str:
    """Simple LLM call without tools — used for summarization.
    Retries on rate limits to avoid crashing the agent during context compaction."""
    import random
    profile = config.resolve_model_profile("fast")
    adapter = ProviderAdapter(profile.provider)
    for attempt in range(4):
        try:
            resp = get_client().chat.completions.create(**adapter.chat_kwargs(
                profile=profile,
                messages=messages,
                max_tokens=10000,
            ))
            return resp.choices[0].message.content or ""
        except Exception as e:
            err_str = str(e)
            if ("rate_limit" in err_str.lower() or "429" in err_str) and attempt < 3:
                wait = min(2 ** (attempt + 1), 30) + random.uniform(0, 3)
                log.warning(f"llm_call_simple rate limited, waiting {wait:.1f}s (attempt {attempt+1}/4)")
                time.sleep(wait)
                continue
            log.error(f"llm_call_simple failed: {e}")
            # Return a minimal summary rather than crashing
            return "[context summarization failed — continuing with truncated context]"
    return "[context summarization failed after retries]"


class Agent:
    """
    A single agent with a system prompt and tool access.

    This is the 'managed agent loop' from the architecture:
    - while loop with llm.call(prompt)
    - tool execution
    - context lifecycle (lightweight compaction / REBUILD_WORKING_CONTEXT)

    Skills are handled via progressive disclosure:
    - Level 1: skill catalog (name + description) is baked into system_prompt
    - Level 2: agent decides to read_skill_file("skills/.../SKILL.md") on its own
    - Level 3: SKILL.md references sub-files, agent reads those too
    No external code decides which skills to load — the agent does.
    """

    def __init__(self, name: str, system_prompt: str, use_tools: bool = True,
                 extra_tool_schemas: list[dict] | None = None,
                 middlewares: list | None = None,
                 time_budget: float | None = None,
                 tool_schemas: list[dict] | None = None,
                  tool_context: ToolContext | None = None,
                  stream_callback=None,
                  prompt_cache_identity: dict[str, str] | None = None):
        self.name = name
        self.system_prompt = system_prompt
        self.use_tools = use_tools
        self.extra_tool_schemas = extra_tool_schemas or []
        self.middlewares = middlewares or []  # list[AgentMiddleware]
        self.time_budget = time_budget
        self.tool_schemas = tool_schemas
        self.allowed_tool_names = _tool_names_from_schemas(tool_schemas) if tool_schemas is not None else None
        self.tool_context = tool_context
        self.stream_callback = stream_callback
        self.prompt_cache_identity = prompt_cache_identity
        self._conversations: weakref.WeakSet[AgentConversation] = weakref.WeakSet()

    def _create_runtime_state(self, task: str) -> AgentRuntimeState:
        return AgentRuntimeState(task_board=TaskBoard(goal=task))

    def run(self, task: str) -> str:
        """
        Execute the agent loop until the model stops calling tools
        or we hit the iteration limit.

        Returns the final assistant text response.
        Writes a JSONL trace file to {WORKSPACE}/.harness/traces/trace_{name}.jsonl
        """
        conversation = self.start_conversation(task)
        try:
            return conversation.run_until_idle()
        finally:
            conversation.close()

    def update_tool_schemas(self, tool_schemas: list[dict]) -> None:
        """Hot-reload tool schemas (e.g. after MCP reconnect).

        Updates the schema list, allowed name set, and invalidates any
        cached prompt-cache key on running conversations only when schemas change.
        """
        if tool_schemas == self.tool_schemas:
            self.allowed_tool_names = _tool_names_from_schemas(tool_schemas)
            return
        self.tool_schemas = tool_schemas
        self.allowed_tool_names = _tool_names_from_schemas(tool_schemas)
        for conversation in list(self._conversations):
            conversation._cached_prompt_cache_key = None

    def start_conversation(self, initial_task: str | None = None) -> "AgentConversation":
        conversation = AgentConversation(self, initial_task)
        self._conversations.add(conversation)
        return conversation


class AgentConversation:
    """Reusable live conversation for interactive CLI sessions."""

    def __init__(self, agent: Agent, initial_task: str | None = None):
        self.agent = agent
        self.trace = TraceWriter(agent.name)
        self.runtime_state = agent._create_runtime_state(initial_task or "")
        if agent.tool_context is not None and agent.tool_context.session_id:
            self.runtime_state.session_id = agent.tool_context.session_id
        self.messages: list[dict] = [{"role": "system", "content": agent.system_prompt}]
        self.client = get_client()
        self.provider: ProviderAdapter = current_adapter()
        self.consecutive_errors = 0
        self.last_text = ""
        self.last_run_streamed_text = False
        self._closed = False
        self._iteration_offset = 0
        self.compaction_gate = CompactionGate()
        self._event_bus = agent.tool_context.event_bus if agent.tool_context is not None else None
        self.fact_tracker = FactTracker()
        self.observation_store = ObservationStore(self._observation_dir())
        self._cached_prompt_cache_key: str | None = None
        self._log_rewrite_version = 0
        self._last_prompt_cache_shape = None
        self._pending_prompt_cache_shape = None
        self._run_conversation_start_middlewares()
        if initial_task is not None:
            self.add_user_turn(initial_task)

    def _observation_dir(self) -> Path:
        session_id = getattr(self.runtime_state, "session_id", None) or "default"
        safe_session_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(session_id))
        if self.agent.tool_context is not None:
            root = self.agent.tool_context.workspace.root
        else:
            root = Path(config.WORKSPACE)
        return root / ".harness" / "observations" / safe_session_id

    def _build_prompt(self) -> list[dict]:
        """Build the messages list that will be sent to the LLM.

        The API prompt is the durable conversation log. Dynamic fact changes
        are represented as appended messages, not as a regenerated prelude.
        """
        return self.messages

    def add_user_turn(self, task: str) -> None:
        self.runtime_state.current_turn_start_index = len(self.messages)
        self.runtime_state.task_board = TaskBoard(goal=task)
        self.runtime_state.action_tool_count = 0
        self.runtime_state.fallback = AgentFallbackState()
        self.runtime_state.auto_compaction_turn_start_index = -1
        self.runtime_state.auto_compaction_suspended = False
        self.runtime_state.context_anxiety_turn_start_index = -1
        for mw in self.agent.middlewares:
            mw.begin_turn(
                task,
                self.messages,
                runtime_state=self.runtime_state,
                agent_name=self.agent.name,
            )
        self._append_message({"role": "user", "content": task})

    def _append_message(self, message: dict) -> None:
        self.messages.append(message)
        self.compaction_gate.bump_revision()

    def _run_conversation_start_middlewares(self) -> None:
        for mw in self.agent.middlewares:
            on_start = getattr(mw, "on_conversation_start", None)
            if on_start is None:
                continue
            injected = on_start(
                self.messages,
                runtime_state=self.runtime_state,
                agent_name=self.agent.name,
            ) or []
            for message in injected:
                self._append_message(message)
                self.trace.middleware_inject(
                    type(mw).__name__,
                    "on_conversation_start",
                    str(message.get("content") or ""),
                )

    def _replace_messages(self, messages: list[dict]) -> None:
        self.messages = list(messages)
        self.observation_store.detach_message_indexes(self.messages)
        self._log_rewrite_version += 1
        self.compaction_gate.bump_revision()

    def _strip_dynamic_context_messages(self) -> bool:
        kept: list[dict] = []
        removed_before_turn = 0
        changed = False
        turn_start = self.runtime_state.current_turn_start_index
        for idx, message in enumerate(self.messages):
            content = str(message.get("content") or "")
            if content.startswith(DYNAMIC_CONTEXT_MARKER):
                changed = True
                if idx < turn_start:
                    removed_before_turn += 1
                continue
            kept.append(message)
        if not changed:
            return False
        self.messages = kept
        self.runtime_state.current_turn_start_index = max(1, turn_start - removed_before_turn)
        self.observation_store.detach_message_indexes(self.messages)
        self.compaction_gate.bump_revision()
        return True

    def _inject_dynamic_context_after_system(self, injected: list[dict], *, source: str) -> None:
        if not injected:
            return
        insert_at = 1 if self.messages and self.messages[0].get("role") == "system" else 0
        for offset, message in enumerate(injected):
            self.messages.insert(insert_at + offset, message)
            self.trace.middleware_inject(
                type(source).__name__ if not isinstance(source, str) else source,
                "on_context_compacted",
                str(message.get("content") or ""),
            )
        if self.runtime_state.current_turn_start_index >= insert_at:
            self.runtime_state.current_turn_start_index += len(injected)
        self.observation_store.detach_message_indexes(self.messages)
        self.compaction_gate.bump_revision()

    def _refresh_dynamic_context_after_compaction(self, *, phase: str) -> None:
        self._strip_dynamic_context_messages()
        for mw in self.agent.middlewares:
            on_compacted = getattr(mw, "on_context_compacted", None)
            if on_compacted is None:
                continue
            injected = on_compacted(
                self.messages,
                runtime_state=self.runtime_state,
                agent_name=self.agent.name,
                phase=phase,
            ) or []
            self._inject_dynamic_context_after_system(injected, source=type(mw).__name__)

    def _session_compacted_dir(self) -> Path | None:
        session_id = getattr(self.runtime_state, "session_id", None)
        if not session_id or session_id == "default":
            return None
        safe_session_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(session_id))
        if self.agent.tool_context is not None:
            root = self.agent.tool_context.workspace.root
        else:
            root = Path(config.WORKSPACE)
        return root / ".harness" / "sessions" / safe_session_id / "compacted"

    def _persist_compacted_summary(self, summary: str, *, phase: str) -> None:
        text = (summary or "").strip()
        if not text:
            return
        compacted_dir = self._session_compacted_dir()
        if compacted_dir is None:
            return
        try:
            history_dir = compacted_dir / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            safe_phase = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in phase) or "compact"
            body = f"# Compacted Context\n\nphase: {phase}\ncreated_at: {datetime.now(timezone.utc).isoformat()}\n\n{text}\n"
            history_path = history_dir / f"{stamp}-{safe_phase}.md"
            history_path.write_text(body, encoding="utf-8")
            (compacted_dir / "latest.md").write_text(body, encoding="utf-8")
        except OSError as exc:
            log.debug("Failed to persist compacted summary: %s", exc)

    def _emit_compaction_started(self, *, token_count: int, threshold: int, phase: str) -> None:
        if self._event_bus is None:
            return
        from ..sessions.events import ContextCompactionStartedEvent
        self._event_bus.emit_event(
            ContextCompactionStartedEvent(
                token_count=token_count,
                threshold=threshold,
                forced=False,
                phase=phase,
            ).to_event()
        )

    def _emit_compaction_committed(self, *, messages_before: int, token_count_before: int,
                                   summary_chars: int = 0, summary_text: str = "",
                                   phase: str = "compact") -> None:
        if summary_text:
            self._persist_compacted_summary(summary_text, phase=phase)
        if self._event_bus is None:
            return
        from ..sessions.events import ContextCompactionCommittedEvent
        tokens_after = context.count_request_tokens(
            self.messages,
            tool_schemas=_tool_schemas_for_agent(self.agent),
        )
        self._event_bus.emit_event(
            ContextCompactionCommittedEvent(
                summary_chars=summary_chars,
                messages_before=messages_before,
                messages_after=len(self.messages),
                tokens_saved=max(0, token_count_before - tokens_after),
            ).to_event()
        )

    def _emit_context_anxiety_observed(self, *, token_count: int, threshold: int, signal) -> None:
        if self._event_bus is None:
            return
        from ..sessions.events import ContextAnxietyObservedEvent
        self._event_bus.emit_event(
            ContextAnxietyObservedEvent(
                token_count=token_count,
                threshold=threshold,
                score=getattr(signal, "score", 0),
                reasons=list(getattr(signal, "reasons", [])),
                source=getattr(signal, "source", "assistant_recent_messages"),
            ).to_event()
        )

    def _maybe_auto_compact(self, agent: Agent, *, token_count: int, thresholds) -> None:
        state = self.runtime_state
        if state.auto_compaction_suspended:
            self.trace.context_event("auto_compaction_suspended", f"tokens={token_count}")
            return
        if state.auto_compaction_turn_start_index == state.current_turn_start_index:
            state.auto_compaction_suspended = True
            self.trace.context_event("auto_compaction_suspended", "already attempted this turn")
            return
        if not self.compaction_gate.can_compact(coalesce_seconds=0):
            return

        state.auto_compaction_turn_start_index = state.current_turn_start_index
        messages_before = len(self.messages)
        token_count_before = token_count
        self._strip_dynamic_context_messages()

        self._emit_compaction_started(
            token_count=token_count,
            threshold=thresholds.compact,
            phase="cleaning_older_outputs",
        )
        cleaned, changed = context.clean_older_tool_outputs(
            self.messages,
            current_turn_start_index=state.current_turn_start_index,
        )
        if changed:
            self._replace_messages(cleaned)
            self.compaction_gate.mark_compacted()
            self._emit_compaction_committed(
                messages_before=messages_before,
                token_count_before=token_count_before,
                phase="cleaning_older_outputs",
            )

        tokens_after_clean = context.count_request_tokens(
            self.messages,
            tool_schemas=_tool_schemas_for_agent(agent),
        )
        token_count_for_summary = tokens_after_clean
        if tokens_after_clean < thresholds.compact:
            self._refresh_dynamic_context_after_compaction(phase="cleaning_older_outputs")
            tokens_after_refresh = context.count_request_tokens(
                self.messages,
                tool_schemas=_tool_schemas_for_agent(agent),
            )
            if tokens_after_refresh < thresholds.compact:
                state.context_refill_streak = 0
                return
            token_count_for_summary = tokens_after_refresh
            self._strip_dynamic_context_messages()

        self._emit_compaction_started(
            token_count=token_count_for_summary,
            threshold=thresholds.compact,
            phase="summarizing_history",
        )
        summarized = context.summarize_older_conversation(
            self.messages,
            llm_call_simple,
            current_turn_start_index=state.current_turn_start_index,
        )
        summary_chars = 0
        summary_text = ""
        if summarized != self.messages:
            summary_text = _first_compacted_summary(summarized)
            summary_chars = len(summary_text)
            self._replace_messages(summarized)
            self.compaction_gate.mark_compacted()
            self._emit_compaction_committed(
                messages_before=messages_before,
                token_count_before=token_count_before,
                summary_chars=summary_chars,
                summary_text=summary_text,
                phase="summarizing_history",
            )
            self._refresh_dynamic_context_after_compaction(phase="summarizing_history")

        tokens_after_summary = context.count_request_tokens(
            self.messages,
            tool_schemas=_tool_schemas_for_agent(agent),
        )
        if tokens_after_summary < thresholds.compact:
            state.context_refill_streak = 0
            return

        state.context_refill_streak += 1
        state.auto_compaction_suspended = True
        self._emit_compaction_started(
            token_count=tokens_after_summary,
            threshold=thresholds.compact,
            phase="auto_compaction_suspended",
        )
        self.trace.context_event("auto_compaction_suspended", f"streak={state.context_refill_streak}")
        if state.context_refill_streak >= 2:
            self._rebuild_working_context(agent, token_count=tokens_after_summary, thresholds=thresholds)

    def _rebuild_working_context(self, agent: Agent, *, token_count: int, thresholds) -> None:
        self._emit_compaction_started(
            token_count=token_count,
            threshold=thresholds.compact,
            phase="rebuilding_working_context",
        )
        messages_before = len(self.messages)
        rebuilt = context.rebuild_working_context(
            self.messages,
            self._working_context_state(),
            current_turn_start_index=self.runtime_state.current_turn_start_index,
            max_turns=5,
        )
        self._replace_messages(rebuilt)
        if context.count_request_tokens(self.messages, tool_schemas=_tool_schemas_for_agent(agent)) >= thresholds.compact:
            rebuilt = context.rebuild_working_context(
                self.messages,
                self._working_context_state(),
                current_turn_start_index=min(self.runtime_state.current_turn_start_index, len(self.messages)),
                max_turns=3,
            )
            self._replace_messages(rebuilt)
        self.runtime_state.current_turn_start_index = max(1, len(self.messages) - 1)
        self.compaction_gate.mark_compacted()
        self.runtime_state.context_refill_streak = 0
        self.runtime_state.auto_compaction_suspended = True
        summary_text = _first_compacted_summary(self.messages)
        self._emit_compaction_committed(
            messages_before=messages_before,
            token_count_before=token_count,
            summary_chars=len(summary_text),
            summary_text=summary_text,
            phase="rebuilding_working_context",
        )
        self._refresh_dynamic_context_after_compaction(phase="rebuilding_working_context")

    def _working_context_state(self) -> dict:
        board = self.runtime_state.task_board
        recent_errors, failed_commands = self._recent_error_state()
        files = list(dict.fromkeys(board.changed_files))
        observed_files = [
            key.removeprefix("file:")
            for observation in self.observation_store.observations
            for key in observation.resource_keys
            if key.startswith("file:")
        ]
        files_touched = list(dict.fromkeys([*files, *observed_files]))
        return {
            "current_user_task": board.goal or self._latest_user_message(),
            "active_plan_status": self._task_board_status(),
            "changed_files": files,
            "files_touched": files_touched,
            "recent_errors": recent_errors,
            "failed_commands": failed_commands,
            "active_constraints": self._active_constraints(),
            "latest_checkpoint_summary": self._latest_checkpoint_summary(),
            "next_recommended_action": board.next_action or "continue from the active task and verify the next smallest change",
        }

    def _latest_user_message(self) -> str:
        for msg in reversed(self.messages):
            if msg.get("role") == "user" and msg.get("content"):
                return str(msg.get("content"))
        return ""

    def _task_board_status(self) -> str:
        board = self.runtime_state.task_board
        parts = []
        if board.current_step:
            parts.append(f"current_step: {board.current_step}")
        if board.completed_steps:
            parts.append("completed: " + ", ".join(board.completed_steps[-5:]))
        if board.blockers:
            parts.append("blockers: " + ", ".join(board.blockers[-5:]))
        if board.result_status:
            parts.append(f"result_status: {board.result_status}")
        return "; ".join(parts) if parts else "none"

    def _recent_error_state(self) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        failed_commands: list[str] = []
        for msg in self.messages[-30:]:
            content = str(msg.get("content") or "")
            lowered = content.lower()
            if "[error]" in lowered or "failed" in lowered or "traceback" in lowered:
                errors.append(content[:500])
            if msg.get("role") == "tool" and ("return_code:" in lowered or "status: failed" in lowered or "[error]" in lowered):
                failed_commands.append(content[:300])
        return errors[-5:], failed_commands[-5:]

    def _active_constraints(self) -> list[str]:
        constraints = []
        for msg in self.messages[-20:]:
            content = str(msg.get("content") or "")
            lowered = content.lower()
            if "constraint" in lowered or "不要" in content or "must" in lowered or "only" in lowered:
                constraints.append(content[:300])
        return constraints[-5:]

    def _latest_checkpoint_summary(self) -> str:
        if self.agent.tool_context is not None:
            root = self.agent.tool_context.workspace.root
        else:
            root = Path(config.WORKSPACE)
        for rel in ("progress.md", ".harness/checkpoints/latest.md"):
            path = root / rel
            if path.exists() and path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        return text[:4_000]
                except OSError:
                    continue
        return "none"

    def submit(self, task: str, cancellation_token=None) -> str:
        self.add_user_turn(task)
        return self.run_until_idle(cancellation_token=cancellation_token)

    def _check_cancelled(self, cancellation_token) -> None:
        if cancellation_token is not None:
            cancellation_token.check()

    def _request_assistant_message(self, kwargs: dict, cancellation_token=None) -> tuple[dict, str | None] | None:
        if self.agent.stream_callback is not None:
            stream_kwargs = dict(kwargs)
            stream_kwargs["stream"] = True
            call_id = self._next_llm_call_id()
            request_started = time.perf_counter()
            first_token_ms: int | None = None
            self._emit_llm_request_started(call_id, streamed=True, kwargs=stream_kwargs)
            saw_chunk = False
            thought_start_time: float | None = None

            def on_chunk() -> None:
                nonlocal saw_chunk
                saw_chunk = True

            def on_text_delta(delta: str) -> None:
                nonlocal first_token_ms
                if first_token_ms is None:
                    first_token_ms = int((time.perf_counter() - request_started) * 1000)
                    self._emit_llm_first_token(call_id, first_token_ms, kwargs=stream_kwargs)
                self.last_run_streamed_text = True
                self.agent.stream_callback(delta)

            def on_reasoning_start() -> None:
                nonlocal thought_start_time
                thought_start_time = time.time()
                if self._event_bus is not None:
                    from ..sessions.events import ThoughtStartedEvent
                    self._event_bus.emit_event(ThoughtStartedEvent().to_event())

            def on_reasoning_delta(delta: str) -> None:
                pass  # Reasoning content collected silently, not displayed

            try:
                if self.provider.supports_prompt_cache_key:
                    stream_kwargs.setdefault("stream_options", {"include_usage": True})
                stream = self.client.chat.completions.create(**stream_kwargs)
                result = self.provider.assistant_message_from_stream(
                    stream,
                    on_text_delta=on_text_delta,
                    on_chunk=on_chunk,
                    on_reasoning_start=on_reasoning_start,
                    on_reasoning_delta=on_reasoning_delta,
                    cancellation_token=cancellation_token,
                )
                # Emit thought finished event if reasoning was detected
                if thought_start_time is not None and self._event_bus is not None:
                    duration = time.time() - thought_start_time
                    from ..sessions.events import ThoughtFinishedEvent
                    self._event_bus.emit_event(
                        ThoughtFinishedEvent(
                            duration_seconds=duration,
                            source=self.provider.name,
                        ).to_event()
                    )
                self._emit_llm_response_finished(
                    call_id,
                    int((time.perf_counter() - request_started) * 1000),
                    finish_reason=result.finish_reason,
                    streamed=True,
                    first_token_ms=first_token_ms,
                    kwargs=stream_kwargs,
                )
                self._emit_llm_usage(result.usage, kwargs.get("prompt_cache_key"))
                return result.assistant_message, result.finish_reason
            except CancelledError:
                raise
            except Exception as exc:
                if saw_chunk:
                    raise
                self.trace.error("stream_fallback", str(exc))

        self._check_cancelled(cancellation_token)
        call_id = self._next_llm_call_id()
        request_started = time.perf_counter()
        self._emit_llm_request_started(call_id, streamed=False, kwargs=kwargs)
        response = self.client.chat.completions.create(**kwargs)
        self._check_cancelled(cancellation_token)
        if not response.choices:
            return None
        choice = response.choices[0]
        self._emit_llm_response_finished(
            call_id,
            int((time.perf_counter() - request_started) * 1000),
            finish_reason=choice.finish_reason,
            streamed=False,
            first_token_ms=None,
            kwargs=kwargs,
        )
        self._emit_llm_usage(_usage_to_dict(getattr(response, "usage", None)), kwargs.get("prompt_cache_key"))

        return self.provider.assistant_message_from_response(choice.message), choice.finish_reason

    def _next_llm_call_id(self) -> str:
        return f"{self.agent.name}-{time.time_ns()}"

    def _emit_llm_request_started(self, call_id: str, *, streamed: bool, kwargs: dict) -> None:
        if self._event_bus is None:
            return
        from ..sessions.events import LlmRequestStartedEvent

        self._event_bus.emit_event(
            LlmRequestStartedEvent(
                call_id=call_id,
                provider=self.provider.name,
                model=str(kwargs.get("model") or config.MODEL),
                streamed=streamed,
                agent=self.agent.name,
            ).to_event()
        )

    def _emit_llm_first_token(self, call_id: str, elapsed_ms: int, *, kwargs: dict) -> None:
        if self._event_bus is None:
            return
        from ..sessions.events import LlmFirstTokenEvent

        self._event_bus.emit_event(
            LlmFirstTokenEvent(
                call_id=call_id,
                elapsed_ms=elapsed_ms,
                provider=self.provider.name,
                model=str(kwargs.get("model") or config.MODEL),
                agent=self.agent.name,
            ).to_event()
        )

    def _emit_llm_response_finished(
        self,
        call_id: str,
        duration_ms: int,
        *,
        finish_reason: str | None,
        streamed: bool,
        first_token_ms: int | None,
        kwargs: dict,
    ) -> None:
        if self._event_bus is None:
            return
        from ..sessions.events import LlmResponseFinishedEvent

        self._event_bus.emit_event(
            LlmResponseFinishedEvent(
                call_id=call_id,
                duration_ms=max(0, int(duration_ms)),
                provider=self.provider.name,
                model=str(kwargs.get("model") or config.MODEL),
                streamed=streamed,
                finish_reason=finish_reason,
                first_token_ms=first_token_ms,
                agent=self.agent.name,
            ).to_event()
        )

    def _emit_llm_usage(self, usage: dict | None, prompt_cache_key: str | None) -> None:
        fallback = self.runtime_state.fallback
        fallback.llm_call_count += 1
        if usage:
            total_tokens = usage.get("total_tokens")
            if total_tokens is not None:
                try:
                    fallback.total_tokens += int(total_tokens)
                except (TypeError, ValueError):
                    pass
            self._maybe_emit_budget_warning(
                "total_tokens",
                fallback.total_tokens,
                config.MAX_AGENT_TOTAL_TOKENS,
            )
        if not usage or self._event_bus is None:
            return
        from ..sessions.events import LlmUsageEvent

        key_hash = _short_hash(prompt_cache_key) if prompt_cache_key else None
        cache_hit_tokens = int(usage.get("cache_hit_tokens") or usage.get("cached_tokens") or 0)
        cache_miss_tokens = int(usage.get("cache_miss_tokens") or 0)
        cache_shape = self._pending_prompt_cache_shape or capture_prompt_cache_shape(
            self.agent,
            _tool_schemas_for_agent(self.agent),
            log_rewrite_version=self._log_rewrite_version,
        )
        cache_diagnostics = compare_prompt_cache_shapes(
            self._last_prompt_cache_shape,
            cache_shape,
            usage,
        )
        self._last_prompt_cache_shape = cache_shape
        self._pending_prompt_cache_shape = None
        self._event_bus.emit_event(
            LlmUsageEvent(
                provider=getattr(self.provider, "name", "unknown"),
                model=config.MODEL,
                prompt_tokens=usage.get("prompt_tokens"),
                cached_tokens=cache_hit_tokens,
                cache_hit_tokens=cache_hit_tokens,
                cache_miss_tokens=cache_miss_tokens,
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                prompt_cache_key_hash=key_hash,
                cache_diagnostics=cache_diagnostics,
                agent=self.agent.name,
            ).to_event()
        )

    def _limit_enabled(self, limit: int | float | None) -> bool:
        try:
            return limit is not None and float(limit) > 0
        except (TypeError, ValueError):
            return False

    def _maybe_emit_budget_warning(self, limit_type: str, used: int, limit: int) -> None:
        if not self._limit_enabled(limit):
            return
        fallback = self.runtime_state.fallback
        if limit_type in fallback.budget_warnings:
            return
        fraction = config.AGENT_BUDGET_WARN_FRACTION
        if fraction <= 0:
            return
        threshold = max(1, int(float(limit) * min(fraction, 1.0)))
        if used < threshold:
            return
        fallback.budget_warnings.add(limit_type)
        if self._event_bus is None:
            return
        from ..sessions.events import AgentBudgetWarningEvent

        self._event_bus.emit_event(
            AgentBudgetWarningEvent(
                limit_type=limit_type,
                used=int(used),
                limit=int(limit),
                fraction=min(float(used) / float(limit), 999.0),
                agent=self.agent.name,
            ).to_event()
        )

    def _request_token_budget_stop_if_needed(self) -> bool:
        limit = config.MAX_AGENT_TOTAL_TOKENS
        fallback = self.runtime_state.fallback
        if not self._limit_enabled(limit) or fallback.total_tokens <= limit:
            return False
        fallback.request_stop(
            reason="token_budget_exceeded",
            limit_type="total_tokens",
            used=fallback.total_tokens,
            limit=limit,
            recent_action_summary=fallback.recent_action_summary,
        )
        return True

    def _record_tool_call_budget(self, tool_name: str, tool_args: dict) -> bool:
        fallback = self.runtime_state.fallback
        limit = config.MAX_AGENT_TOOL_CALLS
        if self._limit_enabled(limit) and fallback.tool_call_count >= limit:
            fallback.request_stop(
                reason="tool_call_budget_exceeded",
                limit_type="tool_calls",
                used=fallback.tool_call_count,
                limit=limit,
                last_tool=tool_name,
                recent_action_summary=fallback.recent_action_summary,
            )
            return False
        fallback.tool_call_count += 1
        fallback.record_action(_safe_tool_summary(tool_name, tool_args))
        self._maybe_emit_budget_warning("tool_calls", fallback.tool_call_count, limit)
        return True

    def _emit_agent_fallback(self) -> None:
        fallback = self.runtime_state.fallback
        if fallback.fallback_event_emitted or not fallback.stop_requested:
            return
        fallback.fallback_event_emitted = True
        if self._event_bus is None:
            return
        from ..sessions.events import AgentFallbackEvent

        self._event_bus.emit_event(
            AgentFallbackEvent(
                reason=fallback.stop_reason,
                limit_type=fallback.stop_limit_type or None,
                used=fallback.stop_used,
                limit=fallback.stop_limit,
                last_tool=fallback.stop_last_tool or None,
                fingerprint_hash=fallback.stop_fingerprint_hash or None,
                recent_action_summary=fallback.recent_action_summary,
                agent=self.agent.name,
            ).to_event()
        )

    def _fallback_text(self) -> str:
        fallback = self.runtime_state.fallback
        details = [f"Agent fallback triggered: {fallback.stop_reason or 'unknown'}."]
        if fallback.stop_limit_type:
            used = "unknown" if fallback.stop_used is None else str(fallback.stop_used)
            limit = "unknown" if fallback.stop_limit is None else str(fallback.stop_limit)
            details.append(f"{fallback.stop_limit_type}: {used}/{limit}.")
        if fallback.stop_last_tool:
            details.append(f"Last tool: {fallback.stop_last_tool}.")
        details.append("The current turn was stopped to prevent runaway execution; inspect the session events for details.")
        return " ".join(details)

    def _append_blocked_tool_results(self, tool_calls: list, reason: str) -> None:
        status_source = "budget" if "budget" in reason else "fallback"
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_arguments = tc["function"].get("arguments") or "{}"
            try:
                fn_args = json.loads(fn_arguments)
            except json.JSONDecodeError:
                fn_args = {}
            output = f"[blocked] Agent fallback triggered ({reason}); tool was not executed."
            tool_result = finalize_intercepted_tool_result(
                ToolResult(
                    tool=fn_name,
                    status="failed",
                    output=output,
                    error=output.removeprefix("[blocked] "),
                    metadata={"status_source": status_source, "fallback_reason": reason},
                ),
                arguments=fn_args,
                tool_context=self.agent.tool_context,
                agent_name=self.agent.name,
            )
            result = tool_result.to_text()
            self.trace.tool_call(fn_name, fn_args, result)
            self._append_message({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    def run_until_idle(self, cancellation_token=None) -> str:
        agent = self.agent
        self.last_run_streamed_text = False
        self._cancellation_token = cancellation_token

        for local_iteration in range(1, config.MAX_AGENT_ITERATIONS + 1):
            iteration = self._iteration_offset + local_iteration

            # --- Cancellation check ---
            self._check_cancelled(cancellation_token)

            # --- Middleware: per-iteration hooks ---
            for mw in agent.middlewares:
                inject = mw.per_iteration(
                    iteration,
                    self.messages,
                    runtime_state=self.runtime_state,
                    agent_name=agent.name,
                )
                if inject:
                    self._append_message({"role": "user", "content": inject})
                    self.trace.middleware_inject(type(mw).__name__, "per_iteration", inject)

            # --- Context lifecycle check ---
            tool_schemas = _tool_schemas_for_agent(agent)
            token_count = context.count_request_tokens(self.messages, tool_schemas=tool_schemas)
            log.info(f"[{agent.name}] iteration={iteration}  tokens≈{token_count}")
            self.trace.iteration(iteration, token_count)

            thresholds = get_thresholds()
            action = compaction_action(token_count, thresholds)
            anxiety = context.detect_anxiety(self.messages)
            if anxiety and self.runtime_state.context_anxiety_turn_start_index != self.runtime_state.current_turn_start_index:
                self.runtime_state.context_anxiety_turn_start_index = self.runtime_state.current_turn_start_index
                score = getattr(anxiety, "score", 0)
                self.trace.context_event("context_anxiety", f"tokens={token_count} score={score}")
                self._emit_context_anxiety_observed(
                    token_count=token_count,
                    threshold=thresholds.compact,
                    signal=anxiety,
                )
            if action == "auto_compact":
                self._maybe_auto_compact(agent, token_count=token_count, thresholds=thresholds)

            # --- Build prompt and LLM call ---
            prompt_messages = self._build_prompt()
            profile = config.resolve_model_profile(config.MODEL_INTENSITY)
            chat_args = {
                "profile": profile,
                "messages": prompt_messages,
                "max_tokens": 32768,
            }
            if agent.use_tools:
                chat_args["tools"] = tool_schemas
                chat_args["tool_choice"] = "auto"
            else:
                tool_schemas = None
            self._pending_prompt_cache_shape = capture_prompt_cache_shape(
                agent,
                tool_schemas,
                log_rewrite_version=self._log_rewrite_version,
            )
            if self.provider.supports_prompt_cache_key:
                if self._cached_prompt_cache_key is None:
                    self._cached_prompt_cache_key = _prompt_cache_key(agent, tool_schemas)
                chat_args["prompt_cache_key"] = self._cached_prompt_cache_key
            kwargs = self.provider.chat_kwargs(**chat_args)

            try:
                completion = self._request_assistant_message(kwargs, cancellation_token=cancellation_token)
            except CancelledError:
                raise
            except Exception as e:
                err_str = str(e)
                self.trace.error("api_error", err_str)

                # Rate limits get longer backoff and don't count toward abort threshold
                if "rate_limit" in err_str.lower() or "429" in err_str:
                    import random
                    wait = min(2 ** (self.consecutive_errors + 2), 120) + random.uniform(0, 5)
                    log.warning(f"[{agent.name}] Rate limited, waiting {wait:.1f}s...")
                    time.sleep(wait)
                    continue

                log.error(f"[{agent.name}] API error: {e}")
                self.consecutive_errors += 1
                if self.consecutive_errors >= config.MAX_TOOL_ERRORS:
                    log.error(f"[{agent.name}] Too many API errors, aborting.")
                    self.trace.finish("api_errors", iteration)
                    break
                time.sleep(2 ** self.consecutive_errors)
                continue

            self.consecutive_errors = 0

            # --- Guard against empty choices ---
            if completion is None:
                log.warning(f"[{agent.name}] API returned empty choices. Retrying...")
                self.trace.error("empty_choices", "API returned no choices")
                self.consecutive_errors += 1
                if self.consecutive_errors >= config.MAX_TOOL_ERRORS:
                    log.error(f"[{agent.name}] Too many empty responses, aborting.")
                    self.trace.finish("empty_choices", iteration)
                    break
                time.sleep(2)
                continue

            assistant_msg, finish_reason = completion
            content = assistant_msg.get("content")
            tool_calls = assistant_msg.get("tool_calls") or []

            self._check_cancelled(cancellation_token)

            # --- Append assistant message to history ---
            self._append_message(assistant_msg)

            # --- Trace the LLM response ---
            self.trace.llm_response(content, tool_calls, finish_reason)

            # --- If model produced text, capture it ---
            if content:
                self.last_text = content
                log.info(f"[{agent.name}] assistant: {content[:200]}...")

            # --- If no tool calls, check pre-exit middlewares ---
            if not tool_calls:
                forced_continue = False
                for mw in agent.middlewares:
                    inject = mw.pre_exit(
                        self.messages,
                        runtime_state=self.runtime_state,
                        agent_name=agent.name,
                    )
                    if inject:
                        self._append_message({"role": "user", "content": inject})
                        self.trace.middleware_inject(type(mw).__name__, "pre_exit", inject)
                        forced_continue = True
                        break
                if forced_continue:
                    continue
                log.info(f"[{agent.name}] Finished (no more tool calls).")
                self.trace.finish("no_tool_calls", iteration)
                break

            if self._request_token_budget_stop_if_needed():
                self._emit_agent_fallback()
                self.last_text = self._fallback_text()
                self._append_blocked_tool_results(tool_calls, self.runtime_state.fallback.stop_reason)
                self.trace.finish("agent_fallback", iteration)
                break

            # --- Execute tool calls ---
            stop_after_tool_loop = ToolExecutor(
                self,
                cancellation_token=cancellation_token,
            ).execute(tool_calls)

            if stop_after_tool_loop:
                break

            # --- Check finish reason ---
            if finish_reason == "stop":
                log.info(f"[{agent.name}] Finished (stop).")
                self.trace.finish("stop", iteration)
                break

            if finish_reason == "length":
                log.warning(f"[{agent.name}] Output truncated (max_tokens hit).")
                self.trace.error("length_truncated", "max_tokens hit")
                # If tool calls were present, they were already executed above.
                # Only tell the model they weren't executed if none were parsed
                # (i.e. the truncation cut off the tool call JSON itself).
                if tool_calls:
                    self._append_message({
                        "role": "user",
                        "content": (
                            "[SYSTEM] Your response was truncated (token limit), but your tool calls "
                            "WERE executed successfully. The results are above. "
                            "If you had more tool calls planned, continue with the remaining ones now. "
                            "Do NOT re-run the tools that already executed."
                        ),
                    })
                else:
                    self._append_message({
                        "role": "user",
                        "content": (
                            "[SYSTEM] Your last response was cut off because it exceeded the token limit. "
                            "No tool calls were executed. "
                            "Please retry, but split large files into smaller parts:\n"
                            "1. Write the first half of the file with write_file\n"
                            "2. Then write the second half as a separate file or append\n"
                            "Or simplify the implementation to fit in one response."
                        ),
                    })

        else:
            log.warning(f"[{agent.name}] Hit max iterations ({config.MAX_AGENT_ITERATIONS}).")
            self.runtime_state.fallback.request_stop(
                reason="max_iterations",
                limit_type="iterations",
                used=config.MAX_AGENT_ITERATIONS,
                limit=config.MAX_AGENT_ITERATIONS,
                recent_action_summary=self.runtime_state.fallback.recent_action_summary,
            )
            self._emit_agent_fallback()
            self.last_text = self._fallback_text()
            self.trace.finish("max_iterations", config.MAX_AGENT_ITERATIONS)

        self._iteration_offset += local_iteration
        return self.last_text

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for mw in self.agent.middlewares:
            on_close = getattr(mw, "on_conversation_close", None)
            if on_close is None:
                continue
            on_close(
                self.messages,
                runtime_state=self.runtime_state,
                agent_name=self.agent.name,
            )
        if self.runtime_state.shell_session is not None:
            self.runtime_state.shell_session.close()
        if self.runtime_state.shell_job_manager is not None:
            self.runtime_state.shell_job_manager.close()


def _truncate(s: str, n: int) -> str:
    return s[:n] + "..." if len(s) > n else s


def _safe_tool_summary(tool_name: str, tool_args: dict) -> str:
    return f"{tool_name}({_safe_args_preview(tool_args)})"


def _safe_args_preview(tool_args: dict) -> str:
    """Backward-compatible wrapper (200-char limit)."""
    return safe_args_preview(tool_args, max_chars=200)


def _tool_schemas_for_agent(agent: Agent) -> list[dict] | None:
    if not agent.use_tools:
        return None
    if agent.tool_schemas is not None:
        return agent.tool_schemas
    return TOOL_SCHEMAS + agent.extra_tool_schemas


def _first_compacted_summary(messages: list[dict]) -> str:
    for message in messages:
        content = str(message.get("content") or "")
        if not (
            content.startswith("[COMPACTED CONTEXT")
            or content.startswith("[REBUILD_WORKING_CONTEXT]")
        ):
            continue
        _header, _sep, body = content.partition("\n")
        return (body or content).strip()
    return ""


def _tool_names_from_schemas(tool_schemas: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for schema in tool_schemas or []:
        function = schema.get("function") if isinstance(schema, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return names


def _tool_result_from_before_tool_block(tool_name: str, message: str) -> ToolResult:
    status_source = "approval" if message.startswith("[approval_denied]") else "permission"
    return ToolResult(
        tool=tool_name,
        status="failed",
        output=message,
        error=message,
        metadata={"status_source": status_source},
    )


def _assistant_message_from_response(msg) -> dict:
    return current_adapter().assistant_message_from_response(msg)


def _requires_reasoning_content_roundtrip() -> bool:
    return current_adapter().requires_reasoning_content_roundtrip
