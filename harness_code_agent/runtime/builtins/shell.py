"""Shell execution and background shell job tools."""
from __future__ import annotations

import json

from ... import config
from ...workspace.shell_jobs import ShellJobNotFound
from ..tool_registry import ToolExecutionLane, _coerce_tool_lane
from ..tool_result import ToolResult


def run_bash(
    command: str,
    timeout: int = 300,
    runtime_state=None,
    agent_name: str | None = None,
    tool_context=None,
    execution_lane: ToolExecutionLane | str | None = None,
) -> ToolResult:
    """Run a shell command inside the agent's persistent shell session."""
    # Lane classification is the executor's job; a missing lane means serial.
    lane = _coerce_tool_lane(execution_lane) if execution_lane is not None else ToolExecutionLane.SHELL_SERIAL
    if lane == ToolExecutionLane.SHELL_LONG_RUNNING:
        manager = _shell_job_manager(runtime_state)
        if manager is None:
            return ToolResult(
                tool="run_bash",
                status="failed",
                output="[error] No shell job manager available for long-running run_bash command",
                error="No shell job manager available",
                metadata={"status_source": "runtime"},
            )
        job = manager.start(command, early_exit_seconds=0.5)
        metadata = {
            "status_source": "shell_job",
            "job_id": job.job_id,
            "pid": job.pid,
            "job_status": job.status,
            "exit_code": job.exit_code,
        }
        if job.status == "running":
            output = (
                f"Started background shell job {job.job_id} (pid={job.pid}). "
                "Use read_shell_output to inspect logs and stop_shell_job to stop it."
            )
            return ToolResult(tool="run_bash", status="success", output=output, metadata=metadata)
        tail = job.output_tail
        output = f"[error] Long-running command exited immediately as {job.status}."
        if tail:
            output += f"\n\nRecent output:\n{tail}"
        return ToolResult(
            tool="run_bash",
            status="failed",
            output=output,
            error=f"Long-running command exited immediately as {job.status}",
            return_code=job.exit_code,
            metadata=metadata,
        )

    use_temporary_shell = lane in {ToolExecutionLane.SHELL_READ, ToolExecutionLane.SHELL_VERIFY}
    shell_session = None
    owns_shell = False
    if use_temporary_shell:
        from ...workspace.shell_session import PersistentShellSession

        shell_session = PersistentShellSession(_workspace_root(tool_context))
        owns_shell = True
    elif runtime_state is not None:
        shell_session = runtime_state.shell_session
    if shell_session is None:
        return ToolResult(
            tool="run_bash",
            status="failed",
            output="[error] No active shell session for run_bash",
            error="No active shell session for run_bash",
            metadata={"status_source": "runtime"},
        )
    try:
        shell_result = shell_session.run(command, timeout=timeout)
        if shell_result.timed_out:
            output = (
                f"[error] Command timed out after {timeout}s. "
                f"If this command legitimately needs more time (e.g. compilation, training), "
                f"retry with a larger timeout parameter."
            )
            return ToolResult(
                tool="run_bash",
                status="failed",
                output=output,
                error=f"Command timed out after {timeout}s",
                return_code=shell_result.exit_code,
                metadata={"timed_out": True, "status_source": "shell"},
            )
        output = _build_shell_output(shell_result.stdout, shell_result.stderr)
        output = output or "(no output)"
        ok = shell_result.exit_code == 0
        return ToolResult(
            tool="run_bash",
            status="success" if ok else "failed",
            output=output,
            error=None if ok else f"Command exited with code {shell_result.exit_code}",
            return_code=shell_result.exit_code,
            metadata={"timed_out": False, "status_source": "shell"},
        )
    except Exception as e:
        metadata = {"status_source": "exception"}
        if _looks_like_dead_shell_error(e):
            metadata["shell_session_reset"] = _reset_runtime_shell_session(
                runtime_state,
                shell_session,
            )
        return ToolResult(
            tool="run_bash",
            status="failed",
            output=f"[error] {e}",
            error=str(e),
            metadata=metadata,
        )
    finally:
        if owns_shell and shell_session is not None:
            shell_session.close()


def list_shell_jobs(runtime_state=None) -> ToolResult:
    manager = _shell_job_manager(runtime_state)
    if manager is None:
        return ToolResult(
            tool="list_shell_jobs",
            status="failed",
            output="[error] No shell job manager available",
            error="No shell job manager available",
            metadata={"status_source": "runtime"},
        )
    jobs = [
        {
            "job_id": job.job_id,
            "command": job.command,
            "pid": job.pid,
            "status": job.status,
            "exit_code": job.exit_code,
            "started_at": job.started_at,
            "ended_at": job.ended_at,
            "uptime_seconds": job.uptime_seconds(),
        }
        for job in manager.list_jobs()
    ]
    return ToolResult(
        tool="list_shell_jobs",
        status="success",
        output=json.dumps({"jobs": jobs}, ensure_ascii=False),
        metadata={"status_source": "shell_job", "job_count": len(jobs)},
    )


def read_shell_output(job_id: str, max_chars: int = 12_000, runtime_state=None) -> ToolResult:
    manager = _shell_job_manager(runtime_state)
    if manager is None:
        return ToolResult(
            tool="read_shell_output",
            status="failed",
            output="[error] No shell job manager available",
            error="No shell job manager available",
            metadata={"status_source": "runtime", "job_id": job_id},
        )
    max_chars = _clamp_shell_output_chars(max_chars)
    try:
        output = manager.read_output(job_id, max_chars=max_chars)
        job = manager.get(job_id) if hasattr(manager, "get") else None
    except ShellJobNotFound as exc:
        text = f"[error] {exc}"
        return ToolResult(
            tool="read_shell_output",
            status="failed",
            output=text,
            error=str(exc),
            metadata={"status_source": "shell_job", "job_id": job_id},
        )
    header = f"Shell job {job_id}"
    metadata = {"status_source": "shell_job", "job_id": job_id, "max_chars": max_chars}
    if job is not None:
        header += f" status={job.status} pid={job.pid} exit_code={job.exit_code}"
        metadata.update({"job_status": job.status, "pid": job.pid, "exit_code": job.exit_code})
    body = output or "(no output)"
    return ToolResult(
        tool="read_shell_output",
        status="success",
        output=f"{header}\n\n{body}",
        metadata=metadata,
    )


def stop_shell_job(job_id: str, runtime_state=None) -> ToolResult:
    manager = _shell_job_manager(runtime_state)
    if manager is None:
        return ToolResult(
            tool="stop_shell_job",
            status="failed",
            output="[error] No shell job manager available",
            error="No shell job manager available",
            metadata={"status_source": "runtime", "job_id": job_id},
        )
    try:
        job = manager.stop(job_id)
    except ShellJobNotFound as exc:
        text = f"[error] {exc}"
        return ToolResult(
            tool="stop_shell_job",
            status="failed",
            output=text,
            error=str(exc),
            metadata={"status_source": "shell_job", "job_id": job_id},
        )
    return ToolResult(
        tool="stop_shell_job",
        status="success",
        output=f"Shell job {job.job_id} is {job.status} (pid={job.pid}, exit_code={job.exit_code})",
        metadata={
            "status_source": "shell_job",
            "job_id": job.job_id,
            "job_status": job.status,
            "pid": job.pid,
            "exit_code": job.exit_code,
        },
    )


def _shell_job_manager(runtime_state):
    return getattr(runtime_state, "shell_job_manager", None) if runtime_state is not None else None


def _workspace_root(tool_context=None) -> str:
    if tool_context is not None:
        return str(tool_context.workspace.root)
    return config.WORKSPACE


def _clamp_shell_output_chars(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 12_000
    return max(1, min(100_000, parsed))


def _build_shell_output(stdout: str, stderr: str) -> str:
    """Build shell output string from stdout and stderr."""
    stderr = (stderr or "").strip()
    stdout = (stdout or "").strip()
    if stderr:
        if stdout:
            return stdout + "\n\n--- STDERR ---\n" + stderr
        return "--- STDERR ---\n" + stderr
    return stdout


def _looks_like_dead_shell_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "shell failed to become ready" in text


def _reset_runtime_shell_session(runtime_state, shell_session) -> bool:
    if runtime_state is None or shell_session is None:
        return False
    if getattr(runtime_state, "shell_session", None) is not shell_session:
        return False
    try:
        shell_session.close()
    except Exception:
        pass
    runtime_state.shell_session = None
    return True
