"""Controlled parallel tool helper."""
from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from ..permissions import TOOL_PERMISSION_NETWORK_READ, TOOL_PERMISSION_READ
from ..tool_context import ToolContext
from ..tool_registry import ToolExecutionLane
from ..tool_runner import _registry_for_context, execute_tool_result
from ..tool_result import ToolResult


PARALLEL_MAX_TOOL_USES = 8
PARALLEL_MAX_OUTPUT_CHARS = 120_000
PARALLEL_SAFE_LANES = {
    ToolExecutionLane.WORKSPACE_READ,
    ToolExecutionLane.NETWORK_READ,
    ToolExecutionLane.SUBAGENT_READ,
}
PARALLEL_SAFE_PERMISSIONS = {TOOL_PERMISSION_READ, TOOL_PERMISSION_NETWORK_READ}


def parallel(
    tool_uses: list[dict[str, Any]],
    runtime_state=None,
    agent_name: str | None = None,
    tool_context: ToolContext | None = None,
    cancellation_token=None,
) -> ToolResult:
    """Run independent, side-effect-safe tool calls concurrently."""
    prepared, validation_results = _prepare_parallel_tool_uses(tool_uses, tool_context)
    results: list[tuple[int, str, str, ToolResult]] = list(validation_results)

    if prepared:
        workers = min(len(prepared), PARALLEL_MAX_TOOL_USES)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    execute_tool_result,
                    name,
                    args,
                    runtime_state=runtime_state,
                    agent_name=agent_name,
                    tool_context=tool_context,
                    emit_events=False,
                    execution_lane=lane,
                    cancellation_token=cancellation_token,
                ): (index, op_id, name)
                for index, op_id, name, args, lane in prepared
            }
            pending = set(futures)
            while pending:
                if cancellation_token is not None:
                    cancellation_token.check()
                done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    index, op_id, name = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = ToolResult(
                            tool=name,
                            status="failed",
                            output=f"[error] {type(exc).__name__}: {exc}",
                            error=f"{type(exc).__name__}: {exc}",
                            metadata={"status_source": "exception"},
                        )
                    results.append((index, op_id, name, result))

    results.sort(key=lambda item: item[0])
    return _parallel_result(results)


def _prepare_parallel_tool_uses(
    tool_uses: list[dict[str, Any]],
    tool_context: ToolContext | None,
) -> tuple[
    list[tuple[int, str, str, dict[str, Any], ToolExecutionLane]],
    list[tuple[int, str, str, ToolResult]],
]:
    prepared: list[tuple[int, str, str, dict[str, Any], ToolExecutionLane]] = []
    validation_results: list[tuple[int, str, str, ToolResult]] = []
    if not isinstance(tool_uses, list) or not tool_uses:
        validation_results.append((
            1,
            "op_1",
            "parallel",
            _parallel_validation_error("parallel requires a non-empty tool_uses list"),
        ))
        return prepared, validation_results
    if len(tool_uses) > PARALLEL_MAX_TOOL_USES:
        validation_results.append((
            1,
            "op_1",
            "parallel",
            _parallel_validation_error(f"parallel accepts at most {PARALLEL_MAX_TOOL_USES} tool uses"),
        ))
        return prepared, validation_results

    registry = _registry_for_context(tool_context)
    allowed_permissions = getattr(tool_context, "allowed_tool_permissions", None) if tool_context is not None else None
    blocked_names = getattr(tool_context, "blocked_tool_names", set()) if tool_context is not None else set()
    revealed_names = getattr(tool_context, "revealed_tool_names", set()) if tool_context is not None else set()

    for index, raw in enumerate(tool_uses, start=1):
        if not isinstance(raw, dict):
            validation_results.append((index, f"op_{index}", "unknown", _parallel_validation_error("tool use must be an object")))
            continue
        op_id = str(raw.get("id") or f"op_{index}").strip() or f"op_{index}"
        name = _tool_use_name(raw)
        args = _tool_use_arguments(raw)
        if not name:
            validation_results.append((index, op_id, "unknown", _parallel_validation_error("tool_name is required")))
            continue
        if name == "parallel":
            validation_results.append((index, op_id, name, _parallel_validation_error("parallel cannot call itself")))
            continue
        if name in blocked_names:
            validation_results.append((index, op_id, name, _parallel_validation_error(f"tool is blocked for this profile: {name}")))
            continue
        if registry.get(name) is None:
            validation_results.append((index, op_id, name, _parallel_validation_error(f"unknown tool: {name}")))
            continue
        permission = registry.permission_for(name)
        lane = registry.lane_for(name) or ToolExecutionLane.CONTROL_SERIAL
        disclosure = registry.disclosure_for(name)
        if allowed_permissions is not None and permission not in allowed_permissions:
            validation_results.append((index, op_id, name, _parallel_validation_error(f"tool permission is not allowed: {permission}")))
            continue
        if disclosure == "deferred" and name not in revealed_names:
            validation_results.append((index, op_id, name, _parallel_validation_error(f"deferred tool has not been revealed: {name}")))
            continue
        if permission not in PARALLEL_SAFE_PERMISSIONS or lane not in PARALLEL_SAFE_LANES:
            validation_results.append((
                index,
                op_id,
                name,
                _parallel_validation_error(
                    f"tool is not safe for parallel execution: permission={permission}, lane={lane.value}"
                ),
            ))
            continue
        prepared.append((index, op_id, name, args, lane))

    return prepared, validation_results


def _tool_use_name(raw: dict[str, Any]) -> str:
    value = raw.get("tool_name") or raw.get("tool") or raw.get("name") or raw.get("recipient_name")
    text = str(value or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _tool_use_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    args = raw.get("arguments")
    if args is None:
        args = raw.get("args")
    if args is None:
        args = raw.get("parameters")
    return dict(args) if isinstance(args, dict) else {}


def _parallel_validation_error(message: str) -> ToolResult:
    return ToolResult(
        tool="parallel",
        status="failed",
        output=f"[error] {message}",
        error=message,
        metadata={"status_source": "validation"},
    )


def _parallel_result(results: list[tuple[int, str, str, ToolResult]]) -> ToolResult:
    if not results:
        return ToolResult(
            tool="parallel",
            status="failed",
            output="[error] no parallel tool uses were executed.",
            error="no parallel tool uses",
            metadata={"status_source": "validation"},
        )

    success_count = sum(1 for _, _, _, result in results if result.status == "success")
    failed_count = sum(1 for _, _, _, result in results if result.status == "failed")
    sections = [
        _format_section(index, op_id, name, result)
        for index, op_id, name, result in results
    ]
    output = "\n\n".join(sections)
    truncated = False
    if len(output) > PARALLEL_MAX_OUTPUT_CHARS:
        output = (
            output[:PARALLEL_MAX_OUTPUT_CHARS]
            + f"\n\n[truncated] parallel output exceeded {PARALLEL_MAX_OUTPUT_CHARS} chars."
        )
        truncated = True

    return ToolResult(
        tool="parallel",
        status="success" if success_count else "failed",
        output=output,
        error=None if success_count else "all parallel tool uses failed",
        metadata={
            "status_source": "native",
            "tool_use_count": len(results),
            "success_count": success_count,
            "failed_count": failed_count,
            "truncated": truncated,
            "tool_uses": [
                {
                    "id": op_id,
                    "tool": name,
                    "status": result.status,
                    "error": result.error,
                    "metadata": result.metadata,
                }
                for _, op_id, name, result in results
            ],
        },
    )


def _format_section(index: int, op_id: str, name: str, result: ToolResult) -> str:
    metadata = json.dumps(result.metadata, ensure_ascii=False, sort_keys=True)
    header = f"[{index}] {op_id} kind={name} status={result.status}"
    if result.error:
        header += f" error={result.error}"
    return f"{header}\nmetadata={metadata}\n{result.to_text()}"
