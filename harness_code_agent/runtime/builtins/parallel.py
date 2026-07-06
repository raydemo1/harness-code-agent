"""Dedicated parallel command and delegated-agent helpers."""
from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from ...agent.delegation import READ_ONLY_DELEGATE_PROFILES, delegate_agent
from ...workspace.shell_session import PersistentShellSession
from ..shell_classification import classify_safe_shell_command
from ..tool_context import ToolContext
from ..tool_result import ToolResult


PARALLEL_MAX_ITEMS = 8
PARALLEL_MAX_OUTPUT_CHARS = 120_000


def parallel_commands(
    commands: list[dict[str, Any]],
    tool_context: ToolContext | None = None,
    cancellation_token=None,
) -> ToolResult:
    """Run independent read-only or verification shell commands concurrently."""
    prepared, validation_results = _prepare_commands(commands)
    results: list[tuple[int, str, str, ToolResult]] = list(validation_results)
    workspace = _workspace_root(tool_context)
    if prepared:
        with ThreadPoolExecutor(max_workers=min(len(prepared), PARALLEL_MAX_ITEMS)) as executor:
            futures = {
                executor.submit(_run_parallel_command, command, timeout, workspace): (index, op_id, command)
                for index, op_id, command, timeout in prepared
            }
            _collect_futures(futures, results, cancellation_token)
    return _parallel_result("parallel_commands", results)


def parallel_agents(
    agents: list[dict[str, Any]],
    tool_context: ToolContext | None = None,
    cancellation_token=None,
) -> ToolResult:
    """Run independent read-only delegated agents concurrently."""
    prepared, validation_results = _prepare_agents(agents)
    results: list[tuple[int, str, str, ToolResult]] = list(validation_results)
    if prepared:
        with ThreadPoolExecutor(max_workers=min(len(prepared), PARALLEL_MAX_ITEMS)) as executor:
            futures = {
                executor.submit(
                    delegate_agent,
                    agent_profile=agent_profile,
                    task=task,
                    expected_output=expected_output,
                    allowed_paths=allowed_paths,
                    max_turns=max_turns,
                    max_seconds=max_seconds,
                    tool_context=tool_context,
                ): (index, op_id, agent_profile)
                for index, op_id, agent_profile, task, expected_output, allowed_paths, max_turns, max_seconds in prepared
            }
            _collect_futures(futures, results, cancellation_token)
    return _parallel_result("parallel_agents", results)


def _prepare_commands(commands: list[dict[str, Any]]) -> tuple[list[tuple[int, str, str, int]], list[tuple[int, str, str, ToolResult]]]:
    prepared: list[tuple[int, str, str, int]] = []
    validation: list[tuple[int, str, str, ToolResult]] = []
    if not isinstance(commands, list) or not commands:
        validation.append((1, "op_1", "parallel_commands", _validation_error("parallel_commands", "commands must be a non-empty list")))
        return prepared, validation
    if len(commands) > PARALLEL_MAX_ITEMS:
        validation.append((1, "op_1", "parallel_commands", _validation_error("parallel_commands", f"parallel_commands accepts at most {PARALLEL_MAX_ITEMS} commands")))
        return prepared, validation
    for index, raw in enumerate(commands, start=1):
        if not isinstance(raw, dict):
            validation.append((index, f"op_{index}", "unknown", _validation_error("parallel_commands", "command entry must be an object")))
            continue
        op_id = str(raw.get("id") or f"cmd_{index}").strip() or f"cmd_{index}"
        command = str(raw.get("command") or "").strip()
        if not command:
            validation.append((index, op_id, "run_bash", _validation_error("parallel_commands", "command is required")))
            continue
        safe_kind = classify_safe_shell_command(command)
        if safe_kind not in {"read", "verify"}:
            validation.append((index, op_id, command, _validation_error("parallel_commands", "only read-only or verification commands are allowed")))
            continue
        timeout = _clamp_int(raw.get("timeout"), default=300, minimum=1, maximum=1800)
        prepared.append((index, op_id, command, timeout))
    return prepared, validation


def _prepare_agents(
    agents: list[dict[str, Any]],
) -> tuple[list[tuple[int, str, str, str, str, list[str], int, int]], list[tuple[int, str, str, ToolResult]]]:
    prepared: list[tuple[int, str, str, str, str, list[str], int, int]] = []
    validation: list[tuple[int, str, str, ToolResult]] = []
    if not isinstance(agents, list) or not agents:
        validation.append((1, "op_1", "parallel_agents", _validation_error("parallel_agents", "agents must be a non-empty list")))
        return prepared, validation
    if len(agents) > PARALLEL_MAX_ITEMS:
        validation.append((1, "op_1", "parallel_agents", _validation_error("parallel_agents", f"parallel_agents accepts at most {PARALLEL_MAX_ITEMS} agents")))
        return prepared, validation
    for index, raw in enumerate(agents, start=1):
        if not isinstance(raw, dict):
            validation.append((index, f"op_{index}", "unknown", _validation_error("parallel_agents", "agent entry must be an object")))
            continue
        op_id = str(raw.get("id") or f"agent_{index}").strip() or f"agent_{index}"
        agent_profile = str(raw.get("agent_profile") or raw.get("profile") or "").strip().lower().replace("-", "_")
        task = str(raw.get("task") or "").strip()
        if agent_profile not in READ_ONLY_DELEGATE_PROFILES:
            validation.append((index, op_id, agent_profile or "unknown", _validation_error("parallel_agents", "parallel_agents only allows explore, test_design, review, or verify")))
            continue
        if not task:
            validation.append((index, op_id, agent_profile, _validation_error("parallel_agents", "task is required")))
            continue
        allowed_paths = raw.get("allowed_paths")
        if not isinstance(allowed_paths, list):
            allowed_paths = []
        prepared.append((
            index,
            op_id,
            agent_profile,
            task,
            str(raw.get("expected_output") or ""),
            [str(path) for path in allowed_paths],
            _clamp_int(raw.get("max_turns"), default=6, minimum=1, maximum=20),
            _clamp_int(raw.get("max_seconds"), default=300, minimum=30, maximum=1800),
        ))
    return prepared, validation


def _run_parallel_command(command: str, timeout: int, workspace: Path) -> ToolResult:
    shell = PersistentShellSession(workspace)
    try:
        result = shell.run(command, timeout=timeout)
    finally:
        shell.close()
    output = _command_output(result.stdout, result.stderr) or "(no output)"
    if result.timed_out:
        return ToolResult(
            tool="parallel_commands",
            status="failed",
            output=f"[error] Command timed out after {timeout}s.\n\n{output}",
            error=f"Command timed out after {timeout}s",
            return_code=result.exit_code,
            metadata={"timed_out": True, "status_source": "shell"},
        )
    ok = result.exit_code == 0
    return ToolResult(
        tool="parallel_commands",
        status="success" if ok else "failed",
        output=output,
        error=None if ok else f"Command exited with code {result.exit_code}",
        return_code=result.exit_code,
        metadata={"timed_out": False, "status_source": "shell"},
    )


def _collect_futures(futures: dict, results: list[tuple[int, str, str, ToolResult]], cancellation_token) -> None:
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
                    tool=str(name),
                    status="failed",
                    output=f"[error] {type(exc).__name__}: {exc}",
                    error=f"{type(exc).__name__}: {exc}",
                    metadata={"status_source": "exception"},
                )
            results.append((index, op_id, str(name), result))


def _parallel_result(tool_name: str, results: list[tuple[int, str, str, ToolResult]]) -> ToolResult:
    if not results:
        return ToolResult(tool=tool_name, status="failed", output="[error] no parallel work was executed.", error="no parallel work", metadata={"status_source": "validation"})
    results.sort(key=lambda item: item[0])
    success_count = sum(1 for _, _, _, result in results if result.status == "success")
    failed_count = sum(1 for _, _, _, result in results if result.status == "failed")
    output = "\n\n".join(_format_section(index, op_id, kind, result) for index, op_id, kind, result in results)
    truncated = False
    if len(output) > PARALLEL_MAX_OUTPUT_CHARS:
        output = output[:PARALLEL_MAX_OUTPUT_CHARS] + f"\n\n[truncated] {tool_name} output exceeded {PARALLEL_MAX_OUTPUT_CHARS} chars."
        truncated = True
    return ToolResult(
        tool=tool_name,
        status="success" if success_count else "failed",
        output=output,
        error=None if success_count else "all parallel items failed",
        metadata={
            "status_source": "native",
            "item_count": len(results),
            "success_count": success_count,
            "failed_count": failed_count,
            "truncated": truncated,
            "items": [
                {"id": op_id, "kind": kind, "status": result.status, "error": result.error, "metadata": result.metadata}
                for _, op_id, kind, result in results
            ],
        },
    )


def _format_section(index: int, op_id: str, kind: str, result: ToolResult) -> str:
    metadata = json.dumps(result.metadata, ensure_ascii=False, sort_keys=True)
    header = f"[{index}] {op_id} kind={kind} status={result.status}"
    if result.error:
        header += f" error={result.error}"
    return f"{header}\nmetadata={metadata}\n{result.to_text()}"


def _validation_error(tool_name: str, message: str) -> ToolResult:
    return ToolResult(
        tool=tool_name,
        status="failed",
        output=f"[error] {message}",
        error=message,
        metadata={"status_source": "validation"},
    )


def _workspace_root(tool_context: ToolContext | None = None) -> Path:
    if tool_context is not None:
        return tool_context.workspace.root.resolve()
    from ... import config

    return Path(config.WORKSPACE).resolve()


def _command_output(stdout: str, stderr: str) -> str:
    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    return "\n".join(parts)


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
