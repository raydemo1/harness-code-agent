from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from .. import config
from ..runtime.permissions import PermissionPolicy
from ..runtime import shell_classification
from ..runtime.tool_registry import ToolExecutionLane, tool_schemas_for_profile
from ..runtime.tool_runner import (
    _registry_for_context,
    execute_tool_result,
    finalize_executed_tool_result,
    finalize_intercepted_tool_result,
)
from ..runtime.tool_result import ToolResult
from ..workspace.shell_session import PersistentShellSession

log = logging.getLogger("harness")


MAX_WORKERS = 8
SUBAGENT_LIMIT = 3
VERIFY_SHELL_LIMIT = 2
NETWORK_LIMIT = 2

PARALLEL_LANES = {
    ToolExecutionLane.WORKSPACE_READ,
    ToolExecutionLane.NETWORK_READ,
    ToolExecutionLane.SUBAGENT_READ,
    ToolExecutionLane.SHELL_READ,
    ToolExecutionLane.SHELL_VERIFY,
}


@dataclass
class PreparedToolCall:
    index: int
    tool_call_id: str
    name: str
    args: dict
    lane: ToolExecutionLane
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
    parallel: bool


class ToolExecutor:
    _subagent_semaphore = threading.Semaphore(SUBAGENT_LIMIT)
    _verify_shell_semaphore = threading.Semaphore(VERIFY_SHELL_LIMIT)
    _network_semaphore = threading.Semaphore(NETWORK_LIMIT)

    def __init__(self, conversation, cancellation_token=None):
        self.conversation = conversation
        self.agent = conversation.agent
        self.runtime_state = conversation.runtime_state
        self.cancellation_token = cancellation_token
        self._shell_policy = PermissionPolicy()
        self._tool_calls: list = []
        self._block_remaining_after_index: int | None = None
        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._deferred_user_messages: list[str] = []

    def execute(self, tool_calls: list) -> bool:
        self._tool_calls = list(tool_calls or [])
        self.conversation.compaction_gate.begin_tool_call()
        try:
            prepared, stop = self._prepare_calls(tool_calls)
            if stop:
                return True
            for group in _build_groups(prepared):
                self.conversation._check_cancelled(self.cancellation_token)
                executed = self._execute_group(group)
                stop_after_group = False
                for item in sorted(executed, key=lambda result: result.prepared.index):
                    self._record_executed_result(item)
                    if item.stop_after_tool_loop or self.runtime_state.fallback.stop_requested:
                        stop_after_group = True
                if stop_after_group:
                    self.conversation._emit_agent_fallback()
                    self.conversation.last_text = self.conversation._fallback_text()
                    if self._block_remaining_after_index is not None:
                        self.conversation._append_blocked_tool_results(
                            self._tool_calls[self._block_remaining_after_index:],
                            self.runtime_state.fallback.stop_reason,
                        )
                    self._flush_deferred_user_messages()
                    return True
            self._flush_deferred_user_messages()
            return False
        finally:
            self.conversation.compaction_gate.end_tool_call()
            self._executor.shutdown(wait=False, cancel_futures=True)

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
                        lane=ToolExecutionLane.BLOCKED,
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

            lane, blocked = self._classify_call(fn_name, fn_args)
            if self.agent.allowed_tool_names is not None and fn_name not in self.agent.allowed_tool_names:
                output = f"[blocked] Tool '{fn_name}' is not available to this agent profile."
                blocked = ToolResult(
                    tool=fn_name,
                    status="failed",
                    output=output,
                    error=output.removeprefix("[blocked] "),
                    metadata={"status_source": "permission"},
                )
                lane = ToolExecutionLane.BLOCKED
                self.conversation.trace.middleware_inject("ToolSchemaGuard", "before_tool", output)
            prepared.append(
                PreparedToolCall(
                    index=index,
                    tool_call_id=tc["id"],
                    name=fn_name,
                    args=fn_args,
                    lane=lane,
                    raw=tc,
                    blocked_result=blocked,
                )
            )
        return prepared, False

    def _classify_call(self, name: str, args: dict) -> tuple[ToolExecutionLane, ToolResult | None]:
        registry = _registry_for_context(self.agent.tool_context)
        lane = registry.lane_for(name) or ToolExecutionLane.CONTROL_SERIAL
        if name != "run_bash":
            return lane, None
        command = str(args.get("command", ""))
        shell_risk = self._shell_policy.classify_shell_command(command)
        if shell_risk == "shell_blocked":
            output = "[blocked] blacklisted shell command is never allowed"
            return ToolExecutionLane.BLOCKED, ToolResult(
                tool=name,
                status="failed",
                output=output,
                error=output.removeprefix("[blocked] "),
                metadata={"status_source": "permission", "risk": "shell_blocked"},
            )
        return classify_shell_lane(command), None

    def _execute_group(self, group: ExecutionGroup) -> list[ExecutedToolCall]:
        ready: list[PreparedToolCall] = []
        executed: list[ExecutedToolCall] = []
        for prepared in group.calls:
            self.conversation._check_cancelled(self.cancellation_token)
            if prepared.emit_events and not self.conversation._record_tool_call_budget(prepared.name, prepared.args):
                self._block_remaining_after_index = prepared.index + 1
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
                    self._block_remaining_after_index = prepared.index + 1
                    break
                continue
            for mw in self.agent.middlewares:
                mw.on_tool_allowed(
                    prepared.name,
                    prepared.args,
                    self.conversation.messages,
                    runtime_state=self.runtime_state,
                    agent_name=self.agent.name,
                )
            ready.append(prepared)

        if not ready:
            return executed
        if not group.parallel:
            for prepared in ready:
                executed.append(self._execute_one(prepared))
            return executed

        futures: dict[Future, PreparedToolCall] = {}
        pending: set[Future] = set()
        try:
            for prepared in ready:
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
        for mw in self.agent.middlewares:
            blocked = mw.before_tool(
                prepared.name,
                prepared.args,
                self.conversation.messages,
                runtime_state=self.runtime_state,
                agent_name=self.agent.name,
            )
            if not blocked:
                continue
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
        return None

    def _execute_one(self, prepared: PreparedToolCall) -> ExecutedToolCall:
        semaphore = _semaphore_for(prepared.lane)
        if semaphore is None:
            return self._execute_one_unlimited(prepared)
        with semaphore:
            return self._execute_one_unlimited(prepared)

    def _execute_one_unlimited(self, prepared: PreparedToolCall) -> ExecutedToolCall:
        if prepared.name == "run_bash" and prepared.lane == ToolExecutionLane.SHELL_SERIAL:
            if self.runtime_state.shell_session is None:
                self.runtime_state.shell_session = PersistentShellSession(config.WORKSPACE)
        tool_result = execute_tool_result(
            prepared.name,
            prepared.args,
            runtime_state=self.runtime_state,
            agent_name=self.agent.name,
            tool_context=self.agent.tool_context,
            emit_events=False,
            execution_lane=prepared.lane,
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
            return

        tool_result = finalize_executed_tool_result(
            tool_result,
            arguments=prepared.args,
            tool_context=self.agent.tool_context,
            agent_name=self.agent.name,
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

        for mw in self.agent.middlewares:
            inject = mw.post_tool(
                prepared.name,
                prepared.args,
                result,
                self.conversation.messages,
                runtime_state=self.runtime_state,
                agent_name=self.agent.name,
            )
            if inject:
                self._deferred_user_messages.append(inject)
                self.conversation.trace.middleware_inject(type(mw).__name__, "post_tool", inject)

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


def _build_groups(calls: list[PreparedToolCall]) -> list[ExecutionGroup]:
    groups: list[ExecutionGroup] = []
    current: list[PreparedToolCall] = []
    for call in calls:
        if call.lane in PARALLEL_LANES:
            current.append(call)
            continue
        if current:
            groups.append(ExecutionGroup(current, parallel=True))
            current = []
        groups.append(ExecutionGroup([call], parallel=False))
    if current:
        groups.append(ExecutionGroup(current, parallel=True))
    return groups


def classify_shell_lane(command: str) -> ToolExecutionLane:
    lowered = " ".join(str(command or "").strip().lower().split())
    if not lowered:
        return ToolExecutionLane.SHELL_SERIAL
    if shell_classification.is_long_running_shell_command(lowered):
        return ToolExecutionLane.SHELL_LONG_RUNNING
    safe_kind = shell_classification.classify_safe_shell_command(lowered)
    if safe_kind == "verify":
        return ToolExecutionLane.SHELL_VERIFY
    if safe_kind == "read":
        return ToolExecutionLane.SHELL_READ
    return ToolExecutionLane.SHELL_SERIAL


def _semaphore_for(lane: ToolExecutionLane):
    if lane == ToolExecutionLane.SUBAGENT_READ:
        return ToolExecutor._subagent_semaphore
    if lane == ToolExecutionLane.SHELL_VERIFY:
        return ToolExecutor._verify_shell_semaphore
    if lane == ToolExecutionLane.NETWORK_READ:
        return ToolExecutor._network_semaphore
    return None
