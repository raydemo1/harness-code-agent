"""
Agent implementation — the core while loop with tool use.
Uses OpenAI-compatible chat completions API with function calling.
"""
from __future__ import annotations

import json
import time
import logging
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from . import context
from .compaction import CompactionGate, CompactionManager, get_thresholds, compaction_action
from .observations import (
    FactTracker,
    ObservationStore,
)
from .providers import ProviderAdapter, current_adapter, get_client
from .utils import _prompt_cache_key, _short_hash, _usage_to_dict
from ..runtime import tools
from ..runtime.arg_preview import safe_args_preview
from ..runtime.tool_context import ToolContext
from ..runtime.tool_result import ToolResult
from ..workspace.shell_session import PersistentShellSession
from .cancellation import CancelledError

log = logging.getLogger("harness")


# ---------------------------------------------------------------------------
# Trace writer — records every agent event to a JSONL file
# ---------------------------------------------------------------------------

class TraceWriter:
    """Appends structured events to a JSONL trace file in the harness directory.

    Each line is a JSON object with: timestamp, agent, event_type, and data.
    Trace file: {WORKSPACE}/.harness/traces/trace_{agent_name}.jsonl
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._start_time = time.time()
        trace_dir = Path(config.WORKSPACE) / ".harness" / "traces"
        try:
            trace_dir.mkdir(parents=True, exist_ok=True)
            test_file = trace_dir / f"trace_test_{agent_name}"
            test_file.write_text("test")
            test_file.unlink()
            self._path = trace_dir / f"trace_{agent_name}.jsonl"
        except Exception:
            # Workspace not writable, use harness-agent dir
            self._path = Path(__file__).parent / f"trace_{agent_name}.jsonl"

    def _write(self, event_type: str, data: dict):
        try:
            entry = {
                "t": round(time.time() - self._start_time, 2),
                "agent": self.agent_name,
                "event": event_type,
                **data,
            }
            line = json.dumps(entry, ensure_ascii=False)[:10000]
            # Write to file
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if config.TRACE_STDERR:
                import sys
                print(f"[TRACE] {line}", file=sys.stderr)
        except Exception:
            pass  # never let tracing break the agent

    def iteration(self, n: int, tokens: int):
        self._write("iteration", {"n": n, "tokens": tokens})

    def llm_response(self, content: str | None, tool_calls: list | None, finish_reason: str | None):
        self._write("llm_response", {
            "content": (content or "")[:500],
            "tool_calls": [tc["function"]["name"] for tc in (tool_calls or [])],
            "finish_reason": finish_reason,
        })

    def tool_call(self, name: str, args: dict, result: str):
        self._write("tool_call", {
            "tool": name,
            "args": _truncate(json.dumps(args, ensure_ascii=False), 300),
            "result": _truncate(result, 500),
        })

    def middleware_inject(self, source: str, hook: str, message: str):
        self._write("middleware", {
            "source": source,
            "hook": hook,
            "message": message[:300],
        })

    def context_event(self, event_type: str, reason: str = ""):
        self._write("context", {"type": event_type, "reason": reason})

    def error(self, error_type: str, message: str):
        self._write("error", {"type": error_type, "message": message[:500]})

    def finish(self, reason: str, iterations: int):
        self._write("finish", {"reason": reason, "iterations": iterations})

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


# ---------------------------------------------------------------------------
# Agent runtime state
# ---------------------------------------------------------------------------


@dataclass
class TaskBoard:
    goal: str = ""
    steps: list[str] = field(default_factory=list)
    current_step: str = ""
    completed_steps: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    next_action: str = ""
    planning_mode: str = "unset"
    update_count: int = 0
    action_count: int = 0
    changed_files: list[str] = field(default_factory=list)
    requires_approval: bool = False
    requires_update: bool = False
    needs_final_update: bool = False
    replan_required: bool = False
    replan_reason: str = ""
    plan_revision: int = 0
    result_status: str = ""
    validation: str = ""
    remaining_issues: list[str] = field(default_factory=list)
    actions_since_progress: int = 0


@dataclass
class RecoveryState:
    mode: str = "NORMAL"
    failure_signature: str = ""
    repeat_count: int = 0
    last_successful_action: str = ""
    last_verification_result: str = ""


@dataclass
class AgentFallbackState:
    total_tokens: int = 0
    llm_call_count: int = 0
    tool_call_count: int = 0
    budget_warnings: set[str] = field(default_factory=set)
    stop_requested: bool = False
    stop_reason: str = ""
    stop_limit_type: str = ""
    stop_used: int | None = None
    stop_limit: int | None = None
    stop_last_tool: str = ""
    stop_fingerprint_hash: str = ""
    recent_action_summary: list[str] = field(default_factory=list)
    fallback_event_emitted: bool = False

    def request_stop(
        self,
        *,
        reason: str,
        limit_type: str = "",
        used: int | None = None,
        limit: int | None = None,
        last_tool: str = "",
        fingerprint_hash: str = "",
        recent_action_summary: list[str] | None = None,
    ) -> None:
        if self.stop_requested:
            return
        self.stop_requested = True
        self.stop_reason = reason
        self.stop_limit_type = limit_type
        self.stop_used = used
        self.stop_limit = limit
        self.stop_last_tool = last_tool
        self.stop_fingerprint_hash = fingerprint_hash
        if recent_action_summary is not None:
            self.recent_action_summary = list(recent_action_summary)[-5:]

    def record_action(self, summary: str) -> None:
        summary = summary.strip()
        if not summary:
            return
        self.recent_action_summary.append(summary[:240])
        if len(self.recent_action_summary) > 5:
            self.recent_action_summary = self.recent_action_summary[-5:]


@dataclass
class AgentRuntimeState:
    shell_session: PersistentShellSession | None = None
    task_board: TaskBoard = field(default_factory=TaskBoard)
    recovery: RecoveryState = field(default_factory=RecoveryState)
    fallback: AgentFallbackState = field(default_factory=AgentFallbackState)
    action_tool_count: int = 0
    current_turn_start_index: int = 0
    session_id: str = "default"
    auto_compaction_turn_start_index: int = -1
    auto_compaction_suspended: bool = False
    context_refill_streak: int = 0
    context_anxiety_turn_start_index: int = -1


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------

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
        self.compaction_mgr: CompactionManager | None = None
        self._event_bus = agent.tool_context.event_bus if agent.tool_context is not None else None
        self.fact_tracker = FactTracker()
        self.observation_store = ObservationStore(self._observation_dir())
        self._cached_prompt_cache_key: str | None = None
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

    def _replace_messages(self, messages: list[dict]) -> None:
        self.messages = list(messages)
        self.observation_store.detach_message_indexes(self.messages)
        self.compaction_gate.bump_revision()

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

    def _emit_compaction_committed(self, *, messages_before: int, token_count_before: int, summary_chars: int = 0) -> None:
        if self._event_bus is None:
            return
        from ..sessions.events import ContextCompactionCommittedEvent
        tokens_after = context.count_tokens(self.messages)
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
            self.messages = cleaned
            self.compaction_gate.bump_revision()
            self.compaction_gate.mark_compacted()
            self._emit_compaction_committed(
                messages_before=messages_before,
                token_count_before=token_count_before,
            )

        tokens_after_clean = context.count_tokens(self.messages)
        if tokens_after_clean < thresholds.compact:
            state.context_refill_streak = 0
            return

        self._emit_compaction_started(
            token_count=tokens_after_clean,
            threshold=thresholds.compact,
            phase="summarizing_history",
        )
        summarized = context.summarize_older_conversation(
            self.messages,
            llm_call_simple,
            current_turn_start_index=state.current_turn_start_index,
        )
        summary_chars = 0
        if summarized != self.messages:
            summary_chars = len(summarized[1].get("content", "")) if len(summarized) > 1 else 0
            self._replace_messages(summarized)
            self.compaction_gate.mark_compacted()
            self._emit_compaction_committed(
                messages_before=messages_before,
                token_count_before=token_count_before,
                summary_chars=summary_chars,
            )

        tokens_after_summary = context.count_tokens(self.messages)
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
        rebuilt = context.rebuild_working_context(
            self.messages,
            self._working_context_state(),
            current_turn_start_index=self.runtime_state.current_turn_start_index,
            max_turns=5,
        )
        self._replace_messages(rebuilt)
        if context.count_tokens(self.messages) >= thresholds.compact:
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

    def _working_context_state(self) -> dict:
        board = self.runtime_state.task_board
        recent_errors, failed_commands = self._recent_error_state()
        files = list(dict.fromkeys(board.changed_files))
        return {
            "current_user_task": board.goal or self._latest_user_message(),
            "active_plan_status": self._task_board_status(),
            "changed_files": files,
            "files_touched": files,
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
            saw_chunk = False
            thought_start_time: float | None = None

            def on_chunk() -> None:
                nonlocal saw_chunk
                saw_chunk = True

            def on_text_delta(delta: str) -> None:
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
                self._emit_llm_usage(result.usage, kwargs.get("prompt_cache_key"))
                return result.assistant_message, result.finish_reason
            except CancelledError:
                raise
            except Exception as exc:
                if saw_chunk:
                    raise
                self.trace.error("stream_fallback", str(exc))

        self._check_cancelled(cancellation_token)
        response = self.client.chat.completions.create(**kwargs)
        self._check_cancelled(cancellation_token)
        if not response.choices:
            return None
        choice = response.choices[0]
        self._emit_llm_usage(_usage_to_dict(getattr(response, "usage", None)), kwargs.get("prompt_cache_key"))

        return self.provider.assistant_message_from_response(choice.message), choice.finish_reason

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
        self._event_bus.emit_event(
            LlmUsageEvent(
                provider=getattr(self.provider, "name", "unknown"),
                model=config.MODEL,
                prompt_tokens=usage.get("prompt_tokens"),
                cached_tokens=int(usage.get("cached_tokens") or 0),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                prompt_cache_key_hash=key_hash,
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
            tool_result = tools.finalize_intercepted_tool_result(
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
            token_count = context.count_tokens(self.messages)
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
                if agent.tool_schemas is not None:
                    tool_schemas = agent.tool_schemas
                else:
                    tool_schemas = tools.TOOL_SCHEMAS + agent.extra_tool_schemas
                chat_args["tools"] = tool_schemas
                chat_args["tool_choice"] = "auto"
            else:
                tool_schemas = None
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
            stop_after_tool_loop = False
            for tool_call_index, tc in enumerate(tool_calls):
                self._check_cancelled(cancellation_token)
                self.compaction_gate.begin_tool_call()
                fn_name = tc["function"]["name"]
                fn_arguments = tc["function"].get("arguments") or "{}"
                try:
                    fn_args = json.loads(fn_arguments)
                except json.JSONDecodeError:
                    log.warning(f"[{agent.name}] Bad JSON in tool call {fn_name}: {fn_arguments[:200]}")
                    self.trace.error("bad_json", f"{fn_name}: {fn_arguments[:200]}")
                    self._append_message({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"[error] Invalid JSON arguments: {fn_arguments[:200]}",
                    })
                    self.compaction_gate.end_tool_call()
                    continue

                if not self._record_tool_call_budget(fn_name, fn_args):
                    self._emit_agent_fallback()
                    self.last_text = self._fallback_text()
                    self._append_blocked_tool_results(
                        tool_calls[tool_call_index:],
                        self.runtime_state.fallback.stop_reason,
                    )
                    self.compaction_gate.end_tool_call()
                    stop_after_tool_loop = True
                    break

                intercepted_result = None
                if agent.allowed_tool_names is not None and fn_name not in agent.allowed_tool_names:
                    output = f"[blocked] Tool '{fn_name}' is not available to this agent profile."
                    intercepted_result = tools.finalize_intercepted_tool_result(
                        ToolResult(
                            tool=fn_name,
                            status="failed",
                            output=output,
                            error=output.removeprefix("[blocked] "),
                            metadata={"status_source": "permission"},
                        ),
                        arguments=fn_args,
                        tool_context=agent.tool_context,
                        agent_name=agent.name,
                    )
                    self.trace.middleware_inject("ToolSchemaGuard", "before_tool", output)
                else:
                    for mw in agent.middlewares:
                        blocked = mw.before_tool(
                            fn_name,
                            fn_args,
                            self.messages,
                            runtime_state=self.runtime_state,
                            agent_name=agent.name,
                        )
                        if blocked:
                            blocked_text = blocked.to_text() if isinstance(blocked, ToolResult) else str(blocked)
                            intercepted_result = (
                                blocked
                                if isinstance(blocked, ToolResult)
                                else _tool_result_from_before_tool_block(fn_name, blocked_text)
                            )
                            intercepted_result = tools.finalize_intercepted_tool_result(
                                intercepted_result,
                                arguments=fn_args,
                                tool_context=agent.tool_context,
                                agent_name=agent.name,
                            )
                            self.trace.middleware_inject(type(mw).__name__, "before_tool", blocked_text)
                            break
                if intercepted_result is not None:
                    result = intercepted_result.to_text()
                    self.trace.tool_call(fn_name, fn_args, result)
                    self._append_message({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                    self.compaction_gate.end_tool_call()
                    continue

                if fn_name == "run_bash" and self.runtime_state.shell_session is None:
                    self.runtime_state.shell_session = PersistentShellSession(config.WORKSPACE)

                log.info(f"[{agent.name}] tool: {fn_name}({_truncate(str(fn_args), 120)})")
                tool_result = tools.execute_tool_result(
                    fn_name,
                    fn_args,
                    runtime_state=self.runtime_state,
                    agent_name=agent.name,
                    tool_context=agent.tool_context,
                )
                result = tool_result.to_text()
                log.debug(f"[{agent.name}] tool result: {_truncate(result, 200)}")
                self.trace.tool_call(fn_name, fn_args, result)
                observation = self.observation_store.create(
                    tool=fn_name,
                    args=fn_args,
                    result=tool_result,
                    fact_tracker=self.fact_tracker,
                )

                self._append_message({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": self.observation_store.observed_message(observation, tool_result),
                })
                observation.message_index = len(self.messages) - 1
                invalidation = self.fact_tracker.apply_mutation(
                    tool=fn_name,
                    args=fn_args,
                    result=tool_result,
                    observations=self.observation_store.observations,
                    exclude_ids={observation.id},
                )
                if invalidation:
                    replaced = self.observation_store.replace_long_stale_messages(self.messages)
                    notice = invalidation
                    if replaced:
                        notice += "\nCompressed stale long observations: " + ", ".join(replaced)
                    self._append_message({"role": "user", "content": notice})
                self.compaction_gate.end_tool_call()

                self._check_cancelled(cancellation_token)

                # --- Middleware: post-tool hooks ---
                for mw in agent.middlewares:
                    inject = mw.post_tool(
                        fn_name,
                        fn_args,
                        result,
                        self.messages,
                        runtime_state=self.runtime_state,
                        agent_name=agent.name,
                    )
                    if inject:
                        self._append_message({"role": "user", "content": inject})
                        self.trace.middleware_inject(type(mw).__name__, "post_tool", inject)
                        break

                if self.runtime_state.fallback.stop_requested:
                    self._emit_agent_fallback()
                    self.last_text = self._fallback_text()
                    self.trace.finish("agent_fallback", iteration)
                    stop_after_tool_loop = True
                    break

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
        if self.runtime_state.shell_session is not None:
            self.runtime_state.shell_session.close()
        if self.compaction_mgr is not None:
            self.compaction_mgr.close()


def _truncate(s: str, n: int) -> str:
    return s[:n] + "..." if len(s) > n else s


def _safe_tool_summary(tool_name: str, tool_args: dict) -> str:
    return f"{tool_name}({_safe_args_preview(tool_args)})"


def _safe_args_preview(tool_args: dict) -> str:
    """Backward-compatible wrapper (200-char limit)."""
    return safe_args_preview(tool_args, max_chars=200)


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
