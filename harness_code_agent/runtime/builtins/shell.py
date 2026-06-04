"""Shell execution and background shell job tools."""
from __future__ import annotations

import json

from ... import config
from ...workspace.shell_jobs import ShellJobNotFound
from ..shell_classification import is_long_running_shell_command
from ..tool_registry import ToolExecutionLane, _coerce_tool_lane
from ..tool_result import ToolResult


def run_bash(
    command: str,
    timeout: int = 300,
    runtime_state=None,
    agent_name: str | None = None,
    execution_lane: ToolExecutionLane | str | None = None,
) -> ToolResult:
    """Run a shell command inside the agent's persistent shell session."""
    if execution_lane is not None:
        lane = _coerce_tool_lane(execution_lane)
    elif is_long_running_shell_command(command):
        lane = ToolExecutionLane.SHELL_LONG_RUNNING
    else:
        lane = ToolExecutionLane.SHELL_SERIAL
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
        from ..workspace.shell_session import PersistentShellSession

        shell_session = PersistentShellSession(config.WORKSPACE)
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
        output = _smart_truncate_output(shell_result.stdout, shell_result.stderr)
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
        return ToolResult(
            tool="run_bash",
            status="failed",
            output=f"[error] {e}",
            error=str(e),
            metadata={"status_source": "exception"},
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


def _clamp_shell_output_chars(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 12_000
    return max(1, min(100_000, parsed))


def _smart_truncate_output(stdout: str, stderr: str, limit: int = 12_000) -> str:
    """Truncate command output while preserving the most useful information.

    Strategy:
    - Always keep stderr in full (up to half the budget) — errors live here.
    - Extract lines containing error/warning keywords from the middle of stdout
      that would otherwise be lost in a naive head+tail cut.
    - Use head + important-middle + tail for stdout.
    """
    import re

    stderr = (stderr or "").strip()
    stdout = (stdout or "").strip()
    combined = (stdout + "\n" + stderr).strip() if stderr else stdout

    if len(combined) <= limit:
        return combined

    # Reserve up to 40% of budget for stderr, rest for stdout
    stderr_budget = min(len(stderr), int(limit * 0.4))
    stdout_budget = limit - stderr_budget

    # Truncate stderr if needed (keep tail — most recent errors matter most)
    if len(stderr) > stderr_budget:
        stderr = "...[stderr truncated]\n" + stderr[-(stderr_budget - 30):]

    # Smart-truncate stdout
    if len(stdout) <= stdout_budget:
        truncated_stdout = stdout
    else:
        # Head and tail get 40% each, important middle lines get 20%
        head_size = int(stdout_budget * 0.40)
        tail_size = int(stdout_budget * 0.40)
        middle_budget = stdout_budget - head_size - tail_size - 200  # 200 for markers

        head = stdout[:head_size]
        tail = stdout[-tail_size:]

        # Extract important lines from the middle that would be lost
        middle = stdout[head_size:-tail_size] if tail_size else stdout[head_size:]
        important_lines = []
        _error_pattern = re.compile(
            r'(?i)(error|fail|assert|exception|traceback|warning|not found|denied|refused|fatal)',
        )
        if middle and middle_budget > 0:
            for line in middle.splitlines():
                if _error_pattern.search(line):
                    important_lines.append(line)

        important_section = "\n".join(important_lines)
        if len(important_section) > middle_budget:
            important_section = important_section[:middle_budget]

        middle_part = ""
        if important_section:
            middle_part = (
                f"\n\n[...{len(middle)} chars omitted — key lines extracted:]\n"
                + important_section
                + "\n[...end extracted lines]\n\n"
            )
        else:
            middle_part = (
                f"\n\n[TRUNCATED — {len(middle)} chars omitted from middle]\n\n"
            )

        truncated_stdout = head + middle_part + tail

    if stderr:
        return truncated_stdout + "\n\n--- STDERR ---\n" + stderr
    return truncated_stdout
