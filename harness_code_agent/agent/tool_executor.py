from __future__ import annotations

import json
import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from ..runtime.execution_planner import (
    CallEffect,
    ExecutionPlanner,
    acquire_concurrency,
)
from ..runtime.tool_registry import tool_schemas_for_profile
from ..runtime.tool_result import ToolResult
from ..runtime.tool_runner import (
    _registry_for_context,
    emit_tool_call_started,
    execute_tool_result,
    finalize_executed_tool_result,
    finalize_intercepted_tool_result,
)
from .cancellation import CancelledError

log = logging.getLogger("harness")


MAX_WORKERS = 8


@dataclass
class PreparedToolCall:
    index: int
    tool_call_id: str
    name: str
    args: dict
    effect: CallEffect
    raw: Any
    blocked_result: ToolResult | None = None
    emit_events: bool = True


@dataclass
class ExecutedToolCall:
    prepared: PreparedToolCall
    result: ToolResult
    error: Exception | None = None
    stop_after_tool_loop: bool = False
    intercepted: bool = False


@dataclass
class ExecutionGroup:
    calls: list[PreparedToolCall]
    parallel: bool = True


class ToolExecutor:

    def __init__(self, conversation, cancellation_token=None):
        self.conversation = conversation
        self.agent = conversation.agent
        self.runtime_state = conversation.runtime_state
        self.cancellation_token = cancellation_token
        self._tool_calls: list = []
        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._deferred_user_messages: list[str] = []
        self._middleware_activity: dict[str, dict[str, Any]] = {}

    def execute(self, tool_calls: list) -> bool:
        self._tool_calls = list(tool_calls or [])
        self.conversation.compaction_gate.begin_tool_call()
        try:
            prepared, stop = self._prepare_calls(tool_calls)
            if stop:
                return True
            planner = ExecutionPlanner((call.index, call.effect) for call in prepared)
            by_index = {call.index: call for call in prepared}
            pending = set(by_index)
            completed: set[int] = set()
            buffered: dict[int, ExecutedToolCall] = {}
            next_record = 0
            while pending:
                self.conversation._check_cancelled(self.cancellation_token)
                ready_indexes = planner.ready(pending, completed)
                if not ready_indexes:
                    raise RuntimeError("Tool execution planner produced a dependency cycle")
                safe_indexes = [index for index in ready_indexes if not self._requires_approval(by_index[index])]
                selected = safe_indexes or [ready_indexes[0]]
                executed = self._execute_group(ExecutionGroup([by_index[index] for index in selected]))
                stop_after_group = False
                for item in executed:
                    buffered[item.prepared.index] = item
                    pending.discard(item.prepared.index)
                    completed.add(item.prepared.index)
                    if item.stop_after_tool_loop or self.runtime_state.fallback.stop_requested:
                        stop_after_group = True
                while next_record in buffered:
                    self._record_executed_result(buffered.pop(next_record))
                    next_record += 1
                if stop_after_group:
                    reason = self.runtime_state.fallback.stop_reason
                    for index in sorted(pending):
                        call = by_index[index]
                        output = f"[blocked] Agent fallback triggered ({reason}); tool was not executed."
                        buffered[index] = ExecutedToolCall(
                            call,
                            ToolResult(
                                tool=call.name,
                                status="failed",
                                output=output,
                                error=output.removeprefix("[blocked] "),
                                metadata={"status_source": "budget", "fallback_reason": reason},
                            ),
                            stop_after_tool_loop=True,
                            intercepted=True,
                        )
                    pending.clear()
                    while next_record in buffered:
                        self._record_executed_result(buffered.pop(next_record))
                        next_record += 1
                    self.conversation.emitter.emit_agent_fallback(self.runtime_state.fallback)
                    self.conversation.last_text = self.conversation._fallback_text()
                    self._flush_deferred_user_messages()
                    return True
            self._flush_deferred_user_messages()
            return False
        except CancelledError:
            # The assistant message with tool_calls is already in history;
            # answer every call the API still expects so the next request
            # is not rejected with a 400.
            self._repair_orphaned_tool_calls()
            raise
        finally:
            self.conversation.compaction_gate.end_tool_call()
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _repair_orphaned_tool_calls(self) -> None:
        answered = {
            message.get("tool_call_id")
            for message in self.conversation.messages
            if message.get("role") == "tool"
        }
        orphans = [
            tc
            for tc in self._tool_calls
            if tc.get("id") and tc.get("id") not in answered
        ]
        if not orphans:
            return
        log.info("[%s] Answering %d cancelled tool calls", self.agent.name, len(orphans))
        self.conversation.trace.error("cancelled_tool_calls", f"{len(orphans)} unanswered")
        for tc in orphans:
            self.conversation._append_message({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": "[cancelled] Execution cancelled before this tool produced a result.",
            })

    def _prepare_calls(self, tool_calls: list) -> tuple[list[PreparedToolCall], bool]:
        prepared: list[PreparedToolCall] = []
        for index, tc in enumerate(tool_calls):
            fn_name = tc["function"]["name"]
            fn_arguments = tc["function"].get("arguments") or "{}"
            try:
                fn_args = json.loads(fn_arguments)
            except json.JSONDecodeError:
                log.warning("[%s] Bad JSON in tool call %s: %s", self.agent.name, fn_name, fn_arguments[:200])
                self.conversation.trace.error("bad_json", f"{fn_name}: {fn_arguments[:200]}")
                prepared.append(
                    PreparedToolCall(
                        index=index,
                        tool_call_id=tc["id"],
                        name=fn_name,
                        args={},
                        effect=CallEffect.global_exclusive(kind="blocked"),
                        raw=tc,
                        blocked_result=ToolResult(
                            tool=fn_name,
                            status="failed",
                            output=f"[error] Invalid JSON arguments: {fn_arguments[:200]}",
                            error=f"Invalid JSON arguments: {fn_arguments[:200]}",
                            metadata={"status_source": "validation"},
                        ),
                        emit_events=False,
                    )
                )
                continue

            effect, blocked = self._classify_call(fn_name, fn_args)
            if self.agent.allowed_tool_names is not None and fn_name not in self.agent.allowed_tool_names:
                output = f"[blocked] Tool '{fn_name}' is not available to this agent profile."
                blocked = ToolResult(
                    tool=fn_name,
                    status="failed",
                    output=output,
                    error=output.removeprefix("[blocked] "),
                    metadata={"status_source": "permission"},
                )
                effect = CallEffect.global_exclusive(kind="blocked")
                self.conversation.trace.middleware_inject("ToolSchemaGuard", "before_tool", output)
            prepared.append(
                PreparedToolCall(
                    index=index,
                    tool_call_id=tc["id"],
                    name=fn_name,
                    args=fn_args,
                    effect=effect,
                    raw=tc,
                    blocked_result=blocked,
                )
            )
        return prepared, False

    def _classify_call(self, name: str, args: dict) -> tuple[CallEffect, ToolResult | None]:
        registry = _registry_for_context(self.agent.tool_context)
        return registry.effect_for(name, args, self.agent.tool_context), None

    def _requires_approval(self, prepared: PreparedToolCall) -> bool:
        if prepared.blocked_result is not None:
            return False
        context = self.agent.tool_context
        if context is None:
            return False
        registry = _registry_for_context(context)
        decision = context.permission_policy.decide_tool_call(
            prepared.name,
            prepared.args,
            tool_permission=registry.permission_for(prepared.name),
        )
        return decision.action == "ask"

    def _execute_group(self, group: ExecutionGroup) -> list[ExecutedToolCall]:
        ready: list[PreparedToolCall] = []
        executed: list[ExecutedToolCall] = []
        for prepared in group.calls:
            self.conversation._check_cancelled(self.cancellation_token)
            if prepared.emit_events and not self.conversation._record_tool_call_budget(prepared.name, prepared.args):
                output = (
                    f"[blocked] Agent fallback triggered ({self.runtime_state.fallback.stop_reason}); "
                    "tool was not executed."
                )
                executed.append(
                    ExecutedToolCall(
                        prepared,
                        ToolResult(
                            tool=prepared.name,
                            status="failed",
                            output=output,
                            error=output.removeprefix("[blocked] "),
                            metadata={
                                "status_source": "budget",
                                "fallback_reason": self.runtime_state.fallback.stop_reason,
                            },
                        ),
                        stop_after_tool_loop=True,
                        intercepted=True,
                    )
                )
                break
            if prepared.blocked_result is not None:
                executed.append(ExecutedToolCall(prepared, prepared.blocked_result, intercepted=True))
                continue
            blocked = self._run_before_tool(prepared)
            if blocked is not None:
                executed.append(ExecutedToolCall(prepared, blocked, intercepted=True))
                if self.runtime_state.fallback.stop_requested:
                    break
                continue
            allowed_started = time.perf_counter()
            for mw in self.agent.middlewares:
                mw.on_tool_allowed(
                    prepared.name,
                    prepared.args,
                    self.conversation.messages,
                    runtime_state=self.runtime_state,
                    agent_name=self.agent.name,
                )
            activity = self._middleware_activity.setdefault(
                prepared.tool_call_id,
                _new_middleware_activity(),
            )
            activity["hooks"] += len(self.agent.middlewares)
            activity["duration_ms"] += (
                time.perf_counter() - allowed_started
            ) * 1000
            ready.append(prepared)

        if not ready:
            return executed
        futures: dict[Future, PreparedToolCall] = {}
        pending: set[Future] = set()
        try:
            for prepared in ready:
                if prepared.emit_events:
                    emit_tool_call_started(
                        name=prepared.name,
                        arguments=prepared.args,
                        tool_context=self.agent.tool_context,
                        agent_name=self.agent.name,
                    )
                future = self._executor.submit(self._execute_one, prepared)
                futures[future] = prepared
                pending.add(future)
            while pending:
                self.conversation._check_cancelled(self.cancellation_token)
                done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    prepared = futures[future]
                    try:
                        executed.append(future.result())
                    except Exception as exc:
                        executed.append(
                            ExecutedToolCall(
                                prepared,
                                ToolResult(
                                    tool=prepared.name,
                                    status="failed",
                                    output=f"[error] {type(exc).__name__}: {exc}",
                                    error=f"{type(exc).__name__}: {exc}",
                                    metadata={"status_source": "exception"},
                                ),
                                error=exc,
                            )
                        )
            self.conversation._check_cancelled(self.cancellation_token)
        except Exception:
            for future in pending:
                future.cancel()
            if pending:
                wait(pending, timeout=0.25)
            raise
        return executed

    def _run_before_tool(self, prepared: PreparedToolCall) -> ToolResult | None:
        activity = self._middleware_activity.setdefault(
            prepared.tool_call_id,
            _new_middleware_activity(),
        )
        started = time.perf_counter()
        for mw in self.agent.middlewares:
            activity["hooks"] += 1
            blocked = mw.before_tool(
                prepared.name,
                prepared.args,
                self.conversation.messages,
                runtime_state=self.runtime_state,
                agent_name=self.agent.name,
            )
            if not blocked:
                continue
            activity["outcome"] = "blocked"
            activity["sources"].append(type(mw).__name__)
            activity["duration_ms"] += (time.perf_counter() - started) * 1000
            blocked_text = blocked.to_text() if isinstance(blocked, ToolResult) else str(blocked)
            self.conversation.trace.middleware_inject(type(mw).__name__, "before_tool", blocked_text)
            if isinstance(blocked, ToolResult):
                return blocked
            return ToolResult(
                tool=prepared.name,
                status="failed",
                output=blocked_text,
                error=blocked_text,
                metadata={"status_source": "approval" if blocked_text.startswith("[approval_denied]") else "permission"},
            )
        activity["duration_ms"] += (time.perf_counter() - started) * 1000
        return None

    def _execute_one(self, prepared: PreparedToolCall) -> ExecutedToolCall:
        context = self.agent.tool_context
        resource_guard = context.resource_coordinator.acquire(prepared.effect.resources) if context is not None else nullcontext()
        with acquire_concurrency(prepared.effect.concurrency_key), resource_guard:
            return self._execute_one_unlimited(prepared)

    def _execute_one_unlimited(self, prepared: PreparedToolCall) -> ExecutedToolCall:
        tool_result = execute_tool_result(
            prepared.name,
            prepared.args,
            runtime_state=self.runtime_state,
            agent_name=self.agent.name,
            tool_context=self.agent.tool_context,
            emit_events=False,
            cancellation_token=self.cancellation_token,
        )
        return ExecutedToolCall(prepared, tool_result)

    def _record_executed_result(self, item: ExecutedToolCall) -> None:
        prepared = item.prepared
        tool_result = item.result
        if item.intercepted:
            if prepared.emit_events:
                tool_result = finalize_intercepted_tool_result(
                    tool_result,
                    arguments=prepared.args,
                    tool_context=self.agent.tool_context,
                    agent_name=self.agent.name,
                )
            result = tool_result.to_text()
            self.conversation.trace.tool_call(prepared.name, prepared.args, result)
            self.conversation._append_message({
                "role": "tool",
                "tool_call_id": prepared.tool_call_id,
                "content": result,
            })
            self._emit_middleware_activity(prepared, fallback_outcome="blocked")
            return

        tool_result = finalize_executed_tool_result(
            tool_result,
            arguments=prepared.args,
            tool_context=self.agent.tool_context,
            agent_name=self.agent.name,
            emit_call=False,
        )
        self.runtime_state.execution_facts.record_result(
            prepared.name,
            status=tool_result.status,
            return_code=tool_result.return_code,
            metadata=tool_result.metadata,
        )
        self._reveal_tool_schemas_from_result(tool_result)
        result = tool_result.to_text()
        log.debug("[%s] tool result: %s", self.agent.name, result[:200])
        self.conversation.trace.tool_call(prepared.name, prepared.args, result)
        observation = self.conversation.observation_store.create(
            tool=prepared.name,
            args=prepared.args,
            result=tool_result,
            fact_tracker=self.conversation.fact_tracker,
        )
        self.conversation._append_message({
            "role": "tool",
            "tool_call_id": prepared.tool_call_id,
            "content": self.conversation.observation_store.observed_message(observation, tool_result),
        })
        invalidation = self.conversation.fact_tracker.apply_mutation(
            tool=prepared.name,
            args=prepared.args,
            result=tool_result,
            observations=self.conversation.observation_store.observations,
            exclude_ids={observation.id},
        )
        if invalidation:
            self._deferred_user_messages.append(invalidation)

        post_started = time.perf_counter()
        activity = self._middleware_activity.setdefault(
            prepared.tool_call_id,
            _new_middleware_activity(),
        )
        for mw in self.agent.middlewares:
            activity["hooks"] += 1
            inject = mw.post_tool(
                prepared.name,
                prepared.args,
                tool_result,
                self.conversation.messages,
                runtime_state=self.runtime_state,
                agent_name=self.agent.name,
            )
            if inject:
                activity["outcome"] = "guided"
                activity["sources"].append(type(mw).__name__)
                self._deferred_user_messages.append(inject)
                self.conversation.trace.middleware_inject(type(mw).__name__, "post_tool", inject)
        activity["duration_ms"] += (time.perf_counter() - post_started) * 1000
        self._emit_middleware_activity(prepared)

    def _emit_middleware_activity(
        self,
        prepared: PreparedToolCall,
        *,
        fallback_outcome: str = "passed",
    ) -> None:
        activity = self._middleware_activity.pop(
            prepared.tool_call_id,
            _new_middleware_activity(),
        )
        outcome = str(activity.get("outcome") or fallback_outcome)
        if outcome == "passed" and fallback_outcome != "passed":
            outcome = fallback_outcome
        event_bus = getattr(self.agent.tool_context, "event_bus", None)
        if event_bus is None:
            return
        sources = list(dict.fromkeys(str(item) for item in activity["sources"]))
        event_bus.emit(
            "middleware_activity",
            agent=self.agent.name,
            payload={
                "tool": prepared.name,
                "tool_call_id": prepared.tool_call_id,
                "hooks": int(activity["hooks"]),
                "duration_ms": round(float(activity["duration_ms"]), 1),
                "outcome": outcome,
                "sources": sources,
            },
        )

    def _flush_deferred_user_messages(self) -> None:
        while self._deferred_user_messages:
            content = self._deferred_user_messages.pop(0)
            self.conversation._append_message({"role": "user", "content": content})

    def _reveal_tool_schemas_from_result(self, tool_result: ToolResult) -> None:
        if tool_result.tool != "tool_search":
            return
        raw_names = tool_result.metadata.get("revealed_tool_names")
        if not isinstance(raw_names, list) or self.agent.tool_context is None:
            return
        context = self.agent.tool_context
        registry = context.tool_registry
        if registry is None:
            return
        revealed = context.revealed_tool_names
        new_names = {
            str(name)
            for name in raw_names
            if isinstance(name, str)
            and name not in revealed
            and registry.get(name) is not None
            and registry.disclosure_for(name) == "deferred"
        }
        if not new_names:
            return

        allowed_permissions = context.allowed_tool_permissions
        revealed_schemas = tool_schemas_for_profile(
            allowed_permissions=allowed_permissions,
            include_names=new_names,
            exclude_names=context.blocked_tool_names,
            registry=registry,
            disclosure={"deferred"},
        )
        if not revealed_schemas:
            return

        current_schemas = list(self.agent.tool_schemas or [])
        current_names = {
            schema.get("function", {}).get("name")
            for schema in current_schemas
            if isinstance(schema, dict)
        }
        additions = [
            schema
            for schema in revealed_schemas
            if schema.get("function", {}).get("name") not in current_names
        ]
        if not additions:
            revealed.update(new_names)
            return
        revealed.update(new_names)
        self.agent.update_tool_schemas(current_schemas + additions)


def _new_middleware_activity() -> dict[str, Any]:
    return {
        "hooks": 0,
        "duration_ms": 0.0,
        "outcome": "passed",
        "sources": [],
    }
