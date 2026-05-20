"""
Tool definitions and execution for agents.
Each tool is an OpenAI function-calling schema + a Python implementation.
Agents operate inside config.WORKSPACE to keep generated code isolated.
"""
from __future__ import annotations

import json
import inspect
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import config
from ..sessions.events import FailureEvent, FileChangeEvent, ToolCallEvent, ToolResultEvent
from .approvals import ApprovalRequest
from .tool_context import ToolContext
from .tool_result import ToolResult, unstructured_tool_result_from_text

# Playwright is optional — only needed for browser UI testing
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(path: str) -> Path:
    """Resolve a relative path inside the workspace. Prevent escaping."""
    p = Path(config.WORKSPACE, path).resolve()
    ws = Path(config.WORKSPACE).resolve()
    if not str(p).startswith(str(ws)):
        raise ValueError(f"Path escapes workspace: {path}")
    return p


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def read_file(path: str, tool_context: ToolContext | None = None) -> ToolResult:
    p = tool_context.workspace.resolve(path) if tool_context is not None else _resolve(path)
    if not p.exists():
        return ToolResult(
            tool="read_file",
            status="failed",
            output=f"[error] File not found: {path}",
            error=f"File not found: {path}",
            metadata={"path": path, "status_source": "native"},
        )
    content = p.read_text(encoding="utf-8", errors="replace")
    limit = 60_000
    if len(content) > limit:
        total = len(content)
        content = content[:limit] + (
            f"\n\n[TRUNCATED] You are seeing {limit} of {total} total characters. "
            f"The remaining {total - limit} characters are NOT shown above. "
            f"You MUST use run_bash with head/tail/sed to read the rest if needed."
        )
    return ToolResult(
        tool="read_file",
        status="success",
        output=content,
        metadata={"path": path, "status_source": "native"},
    )


def read_skill_file(path: str) -> ToolResult:
    """Read a file from the skills directory (outside workspace). Path must be relative to project root."""
    project_root = Path(__file__).resolve().parents[2]
    p = (project_root / path).resolve()
    # Must stay within the skills directory
    skills_dir = (project_root / "skills").resolve()
    if not str(p).startswith(str(skills_dir)):
        return ToolResult(
            tool="read_skill_file",
            status="failed",
            output=f"[error] Path must be inside skills/ directory: {path}",
            error=f"Path must be inside skills/ directory: {path}",
            metadata={"path": path, "status_source": "validation"},
        )
    if not p.exists():
        return ToolResult(
            tool="read_skill_file",
            status="failed",
            output=f"[error] Skill file not found: {path}",
            error=f"Skill file not found: {path}",
            metadata={"path": path, "status_source": "native"},
        )
    return ToolResult(
        tool="read_skill_file",
        status="success",
        output=p.read_text(encoding="utf-8", errors="replace")[:60_000],
        metadata={"path": path, "status_source": "native"},
    )


def write_file(
    path: str,
    content: str,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    if not path or not path.strip():
        return ToolResult(
            tool="write_file",
            status="failed",
            output="[error] Empty file path",
            error="Empty file path",
            metadata={"path": path, "status_source": "validation"},
        )
    metadata = {"path": path, "status_source": "native"}
    if tool_context is not None:
        write_result = tool_context.workspace.write_text(path, content)
        rel = write_result.path.relative_to(tool_context.workspace.root)
        metadata["file_changes"] = [
            {
                "path": str(rel),
                "operation": "write_file",
                "snapshot_path": str(write_result.snapshot_path) if write_result.snapshot_path else None,
            }
        ]
    else:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return ToolResult(
        tool="write_file",
        status="success",
        output=f"Wrote {len(content)} chars to {path}",
        metadata=metadata,
    )


def apply_patch(
    path: str,
    search: str,
    replace: str,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    if not path or not str(path).strip():
        return ToolResult(
            tool="apply_patch",
            status="failed",
            output="[error] Empty file path",
            error="Empty file path",
            metadata={"path": path, "status_source": "validation"},
        )
    if tool_context is not None:
        patch_result = tool_context.workspace.apply_text_patch(
            path,
            search=search,
            replace=replace,
        )
        rel = patch_result.path.relative_to(tool_context.workspace.root)
        return ToolResult(
            tool="apply_patch",
            status="success",
            output=f"Patched {path}: replaced {patch_result.replacements} occurrence",
            metadata={
                "path": path,
                "replacements": patch_result.replacements,
                "status_source": "native",
                "file_changes": [
                    {
                        "path": str(rel),
                        "operation": "apply_patch",
                        "snapshot_path": str(patch_result.snapshot_path) if patch_result.snapshot_path else None,
                    }
                ],
            },
        )

    p = _resolve(path)
    if not p.exists():
        return ToolResult(
            tool="apply_patch",
            status="failed",
            output=f"[error] File not found: {path}",
            error=f"File not found: {path}",
            metadata={"path": path, "status_source": "native"},
        )
    original = p.read_text(encoding="utf-8", errors="replace")
    count = original.count(search)
    if count != 1:
        return ToolResult(
            tool="apply_patch",
            status="failed",
            output=f"[error] Patch search text must match exactly once; found {count}",
            error=f"Patch search text must match exactly once; found {count}",
            metadata={"path": path, "status_source": "validation"},
        )
    p.write_text(original.replace(search, replace, 1), encoding="utf-8")
    return ToolResult(
        tool="apply_patch",
        status="success",
        output=f"Patched {path}: replaced 1 occurrence",
        metadata={"path": path, "replacements": 1, "status_source": "native"},
    )


def update_planning_files(
    mode: str,
    goal: str,
    steps: list[str],
    current_step: str,
    completed_steps: list[str],
    blockers: list[str],
    next_action: str,
    task_plan: str | None = None,
    findings: str | None = None,
    runtime_state=None,
    agent_name: str | None = None,
) -> ToolResult:
    """Update planning-with-files state and write the files required by the selected mode."""
    if runtime_state is None:
        return ToolResult(
            tool="update_planning_files",
            status="failed",
            output="[error] update_planning_files requires runtime state",
            error="update_planning_files requires runtime state",
            metadata={"status_source": "runtime"},
        )

    mode = (mode or "").strip().lower()
    goal = (goal or "").strip()
    current_step = (current_step or "").strip()
    next_action = (next_action or "").strip()
    steps = [str(step).strip() for step in (steps or []) if str(step).strip()]
    completed_steps = [str(step).strip() for step in (completed_steps or []) if str(step).strip()]
    blockers = [str(item).strip() for item in (blockers or []) if str(item).strip()]

    if mode not in {"skip", "light", "full"}:
        return ToolResult(
            tool="update_planning_files",
            status="failed",
            output="[error] mode must be one of: skip, light, full",
            error="mode must be one of: skip, light, full",
            metadata={"status_source": "validation"},
        )
    if not goal:
        return ToolResult(
            tool="update_planning_files",
            status="failed",
            output="[error] update_planning_files requires a non-empty goal",
            error="update_planning_files requires a non-empty goal",
            metadata={"status_source": "validation"},
        )
    if not steps:
        return ToolResult(
            tool="update_planning_files",
            status="failed",
            output="[error] update_planning_files requires at least one step",
            error="update_planning_files requires at least one step",
            metadata={"status_source": "validation"},
        )
    if current_step not in steps:
        return ToolResult(
            tool="update_planning_files",
            status="failed",
            output="[error] current_step must be one of the declared steps",
            error="current_step must be one of the declared steps",
            metadata={"status_source": "validation"},
        )
    if any(step not in steps for step in completed_steps):
        return ToolResult(
            tool="update_planning_files",
            status="failed",
            output="[error] completed_steps must be a subset of steps",
            error="completed_steps must be a subset of steps",
            metadata={"status_source": "validation"},
        )
    if not next_action:
        return ToolResult(
            tool="update_planning_files",
            status="failed",
            output="[error] update_planning_files requires a non-empty next_action",
            error="update_planning_files requires a non-empty next_action",
            metadata={"status_source": "validation"},
        )

    board = runtime_state.task_board
    board.goal = goal
    board.steps = steps
    board.current_step = current_step
    board.completed_steps = completed_steps
    board.blockers = blockers
    board.next_action = next_action
    board.update_count += 1
    board.requires_update = False
    board.needs_final_update = False
    board.planning_mode = mode

    progress_lines = [
        "# Progress",
        "",
        "## Planning Mode",
        mode,
        "",
        f"## Goal",
        goal,
        "",
        "## Steps",
    ]
    for step in steps:
        marker = "x" if step in completed_steps else " "
        current = " (current)" if step == current_step else ""
        progress_lines.append(f"- [{marker}] {step}{current}")
    progress_lines.extend([
        "",
        "## Blockers",
    ])
    if blockers:
        progress_lines.extend(f"- {item}" for item in blockers)
    else:
        progress_lines.append("- (none)")
    progress_lines.extend([
        "",
        "## Next Action",
        next_action,
        "",
        f"## Updates",
        str(board.update_count),
    ])

    workspace = Path(config.WORKSPACE)
    written: list[str] = []
    if mode in {"light", "full"}:
        progress_path = workspace / config.PROGRESS_FILE
        progress_path.write_text("\n".join(progress_lines) + "\n", encoding="utf-8")
        written.append(config.PROGRESS_FILE)

    if mode == "full":
        plan_text = (task_plan or "").strip()
        findings_text = (findings or "").strip()
        if not plan_text:
            return ToolResult(
                tool="update_planning_files",
                status="failed",
                output="[error] full mode requires task_plan content",
                error="full mode requires task_plan content",
                metadata={"status_source": "validation"},
            )
        if not findings_text:
            return ToolResult(
                tool="update_planning_files",
                status="failed",
                output="[error] full mode requires findings content",
                error="full mode requires findings content",
                metadata={"status_source": "validation"},
            )
        (workspace / "task_plan.md").write_text(plan_text + "\n", encoding="utf-8")
        (workspace / "findings.md").write_text(findings_text + "\n", encoding="utf-8")
        written.extend(["task_plan.md", "findings.md"])

    if mode == "skip":
        return ToolResult(
            tool="update_planning_files",
            status="success",
            output="Planning mode set to skip; no planning files required",
            metadata={"status_source": "native"},
        )
    return ToolResult(
        tool="update_planning_files",
        status="success",
        output="Updated planning files: " + ", ".join(written),
        metadata={
            "status_source": "native",
            "file_changes": [
                {"path": path, "operation": "write_file", "snapshot_path": None}
                for path in written
            ],
        },
    )


def list_files(directory: str = ".") -> ToolResult:
    p = _resolve(directory)
    if not p.is_dir():
        return ToolResult(
            tool="list_files",
            status="failed",
            output=f"[error] Not a directory: {directory}",
            error=f"Not a directory: {directory}",
            metadata={"directory": directory, "status_source": "native"},
        )
    entries = []
    for item in sorted(p.rglob("*")):
        if item.is_file():
            rel = item.relative_to(Path(config.WORKSPACE).resolve())
            entries.append(str(rel))
    if not entries:
        return ToolResult(
            tool="list_files",
            status="success",
            output="(empty)",
            metadata={"directory": directory, "status_source": "native"},
        )
    return ToolResult(
        tool="list_files",
        status="success",
        output="\n".join(entries[:200]),
        metadata={"directory": directory, "status_source": "native", "count": len(entries)},
    )


def run_bash(
    command: str,
    timeout: int = 300,
    runtime_state=None,
    agent_name: str | None = None,
) -> ToolResult:
    """Run a shell command inside the agent's persistent shell session."""
    if runtime_state is None or runtime_state.shell_session is None:
        return ToolResult(
            tool="run_bash",
            status="failed",
            output="[error] No active shell session for run_bash",
            error="No active shell session for run_bash",
            metadata={"status_source": "runtime"},
        )
    try:
        shell_result = runtime_state.shell_session.run(command, timeout=timeout)
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


def _smart_truncate_output(stdout: str, stderr: str, limit: int = 30_000) -> str:
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


# ---------------------------------------------------------------------------
# Sub-agent delegation (context isolation)
# ---------------------------------------------------------------------------

CONSULTATION_SCOPES = {
    "codebase_investigation",
    "parallel_search",
    "test_design",
    "review",
}


def _tool_schema_by_name(name: str) -> dict | None:
    for schema in TOOL_SCHEMAS:
        if schema.get("function", {}).get("name") == name:
            return schema
    return None


def consultation_tool_schemas() -> list[dict]:
    """Return the read-only tool surface for consultation sub-agents."""
    names = {"read_file", "list_files", "run_bash", "web_search", "web_fetch"}
    return [schema for name in names if (schema := _tool_schema_by_name(name)) is not None]


def planning_tool_schemas() -> list[dict]:
    """Return the read-only tool surface for the plan profile."""
    names = {
        "read_file",
        "list_files",
        "read_skill_file",
        "run_bash",
        "web_search",
        "web_fetch",
        "consult_subagent",
    }
    return [schema for name in names if (schema := _tool_schema_by_name(name)) is not None]


def _as_consultation_report(scope: str, raw_result: str) -> str:
    raw_result = raw_result or ""
    try:
        parsed = json.loads(raw_result)
        if isinstance(parsed, dict) and {"status", "scope", "findings", "evidence", "recommendations", "risks"} <= set(parsed):
            report = parsed
        else:
            raise ValueError("not a consultation report")
    except Exception:
        report = {
            "status": "completed" if raw_result.strip() else "blocked",
            "scope": scope,
            "findings": [raw_result[:7000]] if raw_result.strip() else [],
            "evidence": [],
            "recommendations": [],
            "risks": [],
        }

    text = json.dumps(report, ensure_ascii=False)
    if len(text) > 8000:
        report["findings"] = [
            "\n".join(str(item) for item in report.get("findings", []))[:7000]
            + "\n...(truncated)"
        ]
        text = json.dumps(report, ensure_ascii=False)
    return text


def consult_subagent(task: str, scope: str = "codebase_investigation") -> ToolResult:
    """
    Ask a read-only consultation sub-agent for local findings.

    The main agent owns all code changes, final integration, verification, and
    stopping decisions. Consultation sub-agents may only investigate, search,
    suggest tests, or review; they return a structured report.
    """
    if scope not in CONSULTATION_SCOPES:
        output = "[error] Invalid consultation scope. Use one of: " + ", ".join(sorted(CONSULTATION_SCOPES))
        return ToolResult(
            tool="consult_subagent",
            status="failed",
            output=output,
            error=output.removeprefix("[error] "),
            metadata={"scope": scope, "status_source": "validation"},
        )

    from ..agent.loop import Agent
    from .middlewares import ReadOnlySubagentMiddleware

    sub = Agent(
        name=f"consult_{scope}",
        system_prompt=(
            "You are a read-only consultation helper. You are not a separate implementation owner.\n"
            "You may inspect files, run read-only commands, search, and fetch references. "
            "You must not modify files, start services, install packages, change git state, "
            "or decide whether the overall task is complete.\n"
            "Return only JSON with this shape:\n"
            "{\n"
            '  "status": "completed | blocked",\n'
            f'  "scope": "{scope}",\n'
            '  "findings": ["..."],\n'
            '  "evidence": ["file/path.py:line or command output summary"],\n'
            '  "recommendations": ["..."],\n'
            '  "risks": ["..."]\n'
            "}\n"
            "For test_design scope, provide test cases and assertions only; do not write tests."
        ),
        use_tools=True,
        tool_schemas=consultation_tool_schemas(),
        middlewares=[ReadOnlySubagentMiddleware()],
    )

    result = sub.run(task)
    return ToolResult(
        tool="consult_subagent",
        status="success",
        output=_as_consultation_report(scope, result),
        metadata={"scope": scope, "status_source": "native"},
    )

# ---------------------------------------------------------------------------
# Playwright browser testing
# ---------------------------------------------------------------------------

# Holds a background dev server process so we can start it once and reuse
_dev_server_proc: subprocess.Popen | None = None


def _ensure_dev_server(start_command: str, port: int, startup_wait: int = 8) -> str:
    """Start a dev server in the background if not already running."""
    global _dev_server_proc
    if _dev_server_proc is not None and _dev_server_proc.poll() is None:
        return f"Dev server already running (pid={_dev_server_proc.pid})"
    _dev_server_proc = subprocess.Popen(
        start_command,
        shell=True,
        cwd=config.WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(startup_wait)
    if _dev_server_proc.poll() is not None:
        stderr = _dev_server_proc.stderr.read().decode(errors="replace")[:2000]
        return f"[error] Dev server exited immediately: {stderr}"
    return f"Dev server started (pid={_dev_server_proc.pid}, port={port})"


def stop_dev_server() -> ToolResult:
    """Stop the background dev server."""
    global _dev_server_proc
    if _dev_server_proc is None:
        return ToolResult(
            tool="stop_dev_server",
            status="success",
            output="No dev server running",
            metadata={"status_source": "native"},
        )
    _dev_server_proc.terminate()
    try:
        _dev_server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _dev_server_proc.kill()
    _dev_server_proc = None
    return ToolResult(
        tool="stop_dev_server",
        status="success",
        output="Dev server stopped",
        metadata={"status_source": "native"},
    )


def browser_test(
    url: str,
    actions: list[dict] | None = None,
    screenshot: bool = True,
    start_command: str | None = None,
    port: int = 5173,
    startup_wait: int = 8,
) -> ToolResult:
    """
    Launch a headless browser, navigate to a URL, perform actions, and
    optionally take a screenshot. Returns a text report of what happened.

    actions is a list of dicts, each with:
      - type: "click" | "fill" | "wait" | "evaluate" | "scroll"
      - selector: CSS selector (for click/fill)
      - value: text to type (for fill), JS code (for evaluate)
      - delay: ms to wait (for wait)

    If start_command is provided, starts a dev server first.
    """
    if not HAS_PLAYWRIGHT:
        output = (
            "[error] Playwright not installed. "
            "Install with: pip install playwright && python -m playwright install chromium"
        )
        return ToolResult(
            tool="browser_test",
            status="failed",
            output=output,
            error="Playwright not installed",
            metadata={"status_source": "runtime"},
        )

    report_lines = []
    failed = False
    error_message = None

    # Optionally start dev server
    if start_command:
        srv_result = _ensure_dev_server(start_command, port, startup_wait)
        report_lines.append(f"Server: {srv_result}")
        if srv_result.startswith("[error]"):
            failed = True
            error_message = srv_result.removeprefix("[error] ")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})

            # Navigate
            try:
                page.goto(url, timeout=15000)
                report_lines.append(f"Navigated to {url} — title: {page.title()}")
            except Exception as e:
                report_lines.append(f"[error] Navigation failed: {e}")
                browser.close()
                return ToolResult(
                    tool="browser_test",
                    status="failed",
                    output="\n".join(report_lines),
                    error=f"Navigation failed: {e}",
                    metadata={"url": url, "status_source": "browser"},
                )

            # Check for console errors
            console_errors = []
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

            # Execute actions
            for action in (actions or []):
                action_type = action.get("type", "")
                selector = action.get("selector", "")
                value = action.get("value", "")
                delay = action.get("delay", 1000)

                try:
                    if action_type == "click":
                        page.click(selector, timeout=5000)
                        report_lines.append(f"Clicked: {selector}")
                    elif action_type == "fill":
                        page.fill(selector, value, timeout=5000)
                        report_lines.append(f"Filled '{selector}' with '{value[:50]}'")
                    elif action_type == "wait":
                        page.wait_for_timeout(delay)
                        report_lines.append(f"Waited {delay}ms")
                    elif action_type == "evaluate":
                        result = page.evaluate(value)
                        report_lines.append(f"JS eval result: {str(result)[:500]}")
                    elif action_type == "scroll":
                        page.evaluate(f"window.scrollBy(0, {value or 500})")
                        report_lines.append(f"Scrolled by {value or 500}px")
                    else:
                        report_lines.append(f"[warn] Unknown action type: {action_type}")
                except Exception as e:
                    report_lines.append(f"[error] Action {action_type}('{selector}'): {e}")
                    failed = True
                    error_message = f"Action {action_type}('{selector}'): {e}"

                page.wait_for_timeout(300)  # brief pause between actions

            # Gather page info
            report_lines.append(f"Final URL: {page.url}")
            report_lines.append(f"Visible text (first 2000 chars): {page.inner_text('body')[:2000]}")

            if console_errors:
                report_lines.append(f"Console errors ({len(console_errors)}):")
                for err in console_errors[:10]:
                    report_lines.append(f"  - {err[:200]}")

            # Screenshot
            if screenshot:
                ss_path = Path(config.WORKSPACE) / "_screenshot.png"
                page.screenshot(path=str(ss_path), full_page=False)
                report_lines.append(f"Screenshot saved to _screenshot.png")

            browser.close()

    except Exception as e:
        report_lines.append(f"[error] Browser test failed: {e}")
        failed = True
        error_message = f"Browser test failed: {e}"

    return ToolResult(
        tool="browser_test",
        status="failed" if failed else "success",
        output="\n".join(report_lines),
        error=error_message,
        metadata={"url": url, "status_source": "browser"},
    )


# ---------------------------------------------------------------------------
# OpenAI function-calling schemas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict
    handler: Callable


class ToolRegistry:
    """Thin registry boundary for built-in and future profile-provided tools."""

    def __init__(self):
        self._schemas: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}

    def register(self, schema: dict, handler: Callable) -> None:
        name = schema.get("function", {}).get("name")
        if not name:
            raise ValueError("Tool schema missing function.name")
        self._schemas[name] = schema
        self._handlers[name] = handler

    def get(self, name: str) -> Callable | None:
        return self._handlers.get(name)

    def schemas(self) -> list[dict]:
        return list(self._schemas.values())

    def dispatch(self) -> dict[str, Callable]:
        return dict(self._handlers)


CORE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_file",
            "description": "Read a skill file from the skills/ directory. Use this to load a skill's SKILL.md or any sub-files referenced within it. Path should be relative to project root (e.g. 'skills/frontend-design/SKILL.md').",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path to skill file (e.g. 'skills/frontend-design/SKILL.md')"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "content": {"type": "string", "description": "File content to write"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a safe text patch to one file. The search text must match exactly once, or the patch fails without modifying the file.",
            "parameters": {
                "type": "object",
                "required": ["path", "search", "replace"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "search": {"type": "string", "description": "Existing text to replace. Must match exactly once."},
                    "replace": {"type": "string", "description": "Replacement text"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_planning_files",
            "description": "Set the planning mode and update planning-with-files state. Use skip for <3 estimated tool calls, light for 3-5 tool calls and progress.md only, full for >5 tool calls and task_plan.md/findings.md/progress.md.",
            "parameters": {
                "type": "object",
                "required": ["mode", "goal", "steps", "current_step", "completed_steps", "blockers", "next_action"],
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "Planning mode selected by the agent self-check: skip, light, or full",
                        "enum": ["skip", "light", "full"],
                    },
                    "goal": {"type": "string", "description": "Overall task goal"},
                    "steps": {
                        "type": "array",
                        "description": "Ordered list of planned steps",
                        "items": {"type": "string"},
                    },
                    "current_step": {"type": "string", "description": "The step being worked on now"},
                    "completed_steps": {
                        "type": "array",
                        "description": "Steps already completed",
                        "items": {"type": "string"},
                    },
                    "blockers": {
                        "type": "array",
                        "description": "Current blockers, if any",
                        "items": {"type": "string"},
                    },
                    "next_action": {"type": "string", "description": "The exact next action to take"},
                    "task_plan": {
                        "type": "string",
                        "description": "Full task_plan.md content. Required in full mode; ignored otherwise.",
                    },
                    "findings": {
                        "type": "string",
                        "description": "Full findings.md content. Required in full mode; ignored otherwise.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in a directory recursively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative directory path (default: root)",
                        "default": ".",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a shell command in the workspace directory. Use for installing deps, running builds, starting servers, running tests, etc. For long-running commands (compilation, training), increase the timeout parameter. For background services (VMs, servers), use '... &' and a separate command to check readiness. Stderr is preserved separately in output for easier debugging.",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 300). Increase for long builds/training.",
                        "default": 300,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_subagent",
            "description": (
                "Ask a read-only consultation sub-agent for findings, evidence, recommendations, and risks. "
                "Use only for local codebase investigation, parallel search, test design, or review. "
                "The main agent owns all code changes, final integration, verification, and stop decisions."
            ),
            "parameters": {
                "type": "object",
                "required": ["task"],
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Detailed read-only consultation request",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["codebase_investigation", "parallel_search", "test_design", "review"],
                        "description": "Consultation mode",
                        "default": "codebase_investigation",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information. Use when you need documentation, examples, or domain knowledge not available locally. Returns titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Max results to return (default 5)",
                        "default": 5,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the text content of a web page. Use after web_search to read a specific page in detail.",
            "parameters": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
            },
        },
    },
]

# --- Evaluator-only tools (browser testing) ---

BROWSER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_test",
            "description": (
                "Launch a headless Chromium browser to test the running application. "
                "Navigates to a URL, performs UI actions (click, fill, scroll, evaluate JS), "
                "captures console errors, and takes a screenshot. "
                "Optionally starts a dev server first via start_command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to (e.g. http://localhost:5173)",
                    },
                    "actions": {
                        "type": "array",
                        "description": "List of browser actions to perform sequentially",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["click", "fill", "wait", "evaluate", "scroll"],
                                    "description": "Action type",
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector (for click/fill)",
                                },
                                "value": {
                                    "type": "string",
                                    "description": "Text for fill, JS code for evaluate, pixels for scroll",
                                },
                                "delay": {
                                    "type": "integer",
                                    "description": "Milliseconds to wait (for wait action)",
                                },
                            },
                        },
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": "Take a screenshot after actions (default: true)",
                        "default": True,
                    },
                    "start_command": {
                        "type": "string",
                        "description": "Shell command to start the dev server (e.g. 'npm run dev'). Only needed on first call.",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Port the dev server runs on (default: 5173)",
                        "default": 5173,
                    },
                    "startup_wait": {
                        "type": "integer",
                        "description": "Seconds to wait for dev server to start (default: 8)",
                        "default": 8,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_dev_server",
            "description": "Stop the background dev server started by browser_test.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ---------------------------------------------------------------------------
# Tool-call pre-validation & auto-correction
# ---------------------------------------------------------------------------

def _validate_and_fix(name: str, arguments: dict) -> tuple[dict, str | None]:
    """
    Pre-validate tool arguments and auto-correct common mistakes.
    Returns (fixed_arguments, warning_message_or_None).

    This is a lightweight heuristic layer — no LLM calls.
    Catches the most common tool-call errors from weaker models:
      - Empty/missing required arguments
      - Absolute paths that should be relative
      - Obvious typos in common patterns
    """
    warning = None

    if name == "write_file":
        path = arguments.get("path", "")
        content = arguments.get("content")

        # Empty path
        if not path or not path.strip():
            return arguments, "[auto-fix] Empty file path. You must specify a path."

        # Absolute path → make relative to workspace
        if path.startswith("/"):
            import re
            # Strip common workspace prefixes
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if path.startswith(prefix):
                    arguments["path"] = path[len(prefix):]
                    warning = f"[auto-fix] Converted absolute path '{path}' to relative '{arguments['path']}'"
                    break

        # Missing content
        if content is None:
            arguments["content"] = ""
            warning = "[auto-fix] Missing 'content' argument — writing empty file."

    elif name == "read_file":
        path = arguments.get("path", "")

        # Absolute path → relative
        if path.startswith("/"):
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if path.startswith(prefix):
                    arguments["path"] = path[len(prefix):]
                    warning = f"[auto-fix] Converted absolute path '{path}' to relative '{arguments['path']}'"
                    break

    elif name == "run_bash":
        command = arguments.get("command", "")

        # Empty command
        if not command or not command.strip():
            return arguments, "[auto-fix] Empty command. You must specify a command to run."

        # Detect interactive commands that will hang
        import re
        interactive_cmds = ["vim", "nano", "vi", "less", "more", "top", "htop"]
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word in interactive_cmds:
            return arguments, (
                f"[auto-fix] '{first_word}' is an interactive command that will hang. "
                f"Use non-interactive alternatives: "
                f"for editing use write_file, for viewing use cat/head/tail."
            )

    elif name == "list_files":
        directory = arguments.get("directory", ".")
        if directory.startswith("/"):
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if directory.startswith(prefix):
                    arguments["directory"] = directory[len(prefix):] or "."
                    warning = f"[auto-fix] Converted absolute path '{directory}' to relative '{arguments['directory']}'"
                    break

    return arguments, warning


# ---------------------------------------------------------------------------
# Web search (lightweight, no external deps)
# ---------------------------------------------------------------------------

def web_search(query: str, max_results: int = 5) -> ToolResult:
    """Search the web using DuckDuckGo and return text results.
    Uses DDG's lite HTML endpoint — no API key needed, works in any container.
    """
    import urllib.request
    import urllib.parse
    import re
    import html as html_mod

    try:
        encoded = urllib.parse.urlencode({"q": query})
        url = f"https://lite.duckduckgo.com/lite/?{encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read().decode("utf-8", errors="replace")

        # Extract result links (DDG lite uses rel="nofollow" for result links)
        links = re.findall(
            r'<a[^>]*rel="nofollow"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            raw, re.DOTALL
        )

        # Extract snippets (text in <td> cells that aren't links/navigation)
        cells = re.findall(r'<td[^>]*>(.*?)</td>', raw, re.DOTALL)
        snippets = []
        for cell in cells:
            text = re.sub(r'<[^>]+>', '', cell).strip()
            if len(text) > 50 and not text.startswith('http'):
                snippets.append(text)

        results = []
        for i, (href, title) in enumerate(links):
            if i >= max_results:
                break
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', title).strip())
            # Decode DDG redirect URL
            real_url = href
            m = re.search(r'uddg=([^&]+)', href)
            if m:
                real_url = urllib.parse.unquote(m.group(1))
            snippet = snippets[i] if i < len(snippets) else ""
            results.append(f"{i+1}. {title}\n   {real_url}\n   {snippet[:200]}\n")

        if results:
            return ToolResult(
                tool="web_search",
                status="success",
                output=f"Search results for: {query}\n\n" + "\n".join(results),
                metadata={"query": query, "result_count": len(results), "status_source": "native"},
            )

        return ToolResult(
            tool="web_search",
            status="success",
            output=f"No results found for: {query}",
            metadata={"query": query, "result_count": 0, "status_source": "native"},
        )

    except Exception as e:
        return ToolResult(
            tool="web_search",
            status="failed",
            output=f"[error] Web search failed: {e}",
            error=f"Web search failed: {e}",
            metadata={"query": query, "status_source": "exception"},
        )


def web_fetch(url: str) -> ToolResult:
    """Fetch the content of a web page and return as text."""
    import urllib.request
    import re

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")

        # Strip HTML tags, keep text
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if len(text) > 10000:
            text = text[:10000] + "\n\n[TRUNCATED]"

        return ToolResult(
            tool="web_fetch",
            status="success",
            output=text or "(empty page)",
            metadata={"url": url, "status_source": "native"},
        )

    except Exception as e:
        return ToolResult(
            tool="web_fetch",
            status="failed",
            output=f"[error] Web fetch failed: {e}",
            error=f"Web fetch failed: {e}",
            metadata={"url": url, "status_source": "exception"},
        )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _build_builtin_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    handlers = {
        "read_file": read_file,
        "read_skill_file": read_skill_file,
        "write_file": write_file,
        "apply_patch": apply_patch,
        "update_planning_files": update_planning_files,
        "list_files": list_files,
        "run_bash": run_bash,
        "consult_subagent": consult_subagent,
        "web_search": web_search,
        "web_fetch": web_fetch,
        "browser_test": browser_test,
        "stop_dev_server": stop_dev_server,
    }
    for schema in CORE_TOOL_SCHEMAS + BROWSER_TOOL_SCHEMAS:
        name = schema["function"]["name"]
        if name in handlers:
            registry.register(schema, handlers[name])
    return registry


BUILTIN_TOOL_REGISTRY = _build_builtin_tool_registry()
TOOL_SCHEMAS = CORE_TOOL_SCHEMAS
TOOL_DISPATCH = BUILTIN_TOOL_REGISTRY.dispatch()


def execute_tool(
    name: str,
    arguments: dict,
    runtime_state=None,
    agent_name: str | None = None,
    tool_context: ToolContext | None = None,
) -> str:
    """Execute a tool by name with pre-validation and auto-correction."""
    arguments = dict(arguments or {})

    if tool_context is not None:
        tool_context.event_bus.emit(
            "before_tool",
            agent=agent_name,
            payload={"tool": name, "args": _redact_tool_args(arguments)},
        )
        tool_context.event_bus.emit_event(
            ToolCallEvent(
                tool=name,
                args=_redact_tool_args(arguments),
                agent=agent_name,
            ).to_event()
        )

    fn = BUILTIN_TOOL_REGISTRY.get(name)
    if fn is None:
        return _finalize_tool_result(
            ToolResult(
                tool=name,
                status="failed",
                output=f"[error] Unknown tool: {name}",
                error=f"Unknown tool: {name}",
                metadata={"status_source": "registry"},
            ),
            tool_context=tool_context,
            agent_name=agent_name,
        )

    # Pre-validate and auto-correct arguments
    arguments, fix_warning = _validate_and_fix(name, arguments)

    # If validation returned a blocking error (no fix possible), return it.
    if fix_warning and fix_warning.startswith("[auto-fix] Empty"):
        return _finalize_tool_result(
            ToolResult(
                tool=name,
                status="failed",
                output=fix_warning,
                error=fix_warning,
                metadata={"status_source": "validation"},
            ),
            tool_context=tool_context,
            agent_name=agent_name,
        )
    if fix_warning and "interactive command" in fix_warning:
        return _finalize_tool_result(
            ToolResult(
                tool=name,
                status="failed",
                output=fix_warning,
                error=fix_warning,
                metadata={"status_source": "validation"},
            ),
            tool_context=tool_context,
            agent_name=agent_name,
        )

    if tool_context is not None:
        decision = tool_context.permission_policy.decide_tool_call(name, arguments)
        tool_context.event_bus.emit(
            "permission_decided",
            agent=agent_name,
            payload={
                "tool": name,
                "allowed": decision.allowed,
                "risk": decision.risk,
                "action": decision.action,
                "requires_approval": decision.requires_approval,
                "reason": decision.reason,
            },
        )
        if decision.requires_approval:
            approval_request = ApprovalRequest(
                tool_name=name,
                args=_redact_tool_args(arguments),
                risk=decision.risk,
                reason=decision.reason,
                agent_name=agent_name,
                session_id=tool_context.session_id,
            )
            tool_context.event_bus.emit(
                "approval_requested",
                agent=agent_name,
                payload={
                    "tool": name,
                    "risk": decision.risk,
                    "reason": decision.reason,
                    "args": _redact_tool_args(arguments),
                },
            )
            approval_result = tool_context.approval_provider.request(approval_request)
            tool_context.event_bus.emit(
                "approval_decided",
                agent=agent_name,
                payload={
                    "tool": name,
                    "approved": approval_result.approved,
                    "reason": approval_result.reason,
                    "metadata": approval_result.metadata,
                },
            )
            if not approval_result.approved:
                result = f"[approval_denied] {approval_result.reason}"
                return _finalize_tool_result(
                    ToolResult(
                        tool=name,
                        status="failed",
                        output=result,
                        error=approval_result.reason,
                        metadata={"requires_approval": True, "status_source": "approval"},
                    ),
                    tool_context=tool_context,
                    agent_name=agent_name,
                )
        elif not decision.allowed:
            result = f"[blocked] {decision.reason}"
            return _finalize_tool_result(
                ToolResult(
                    tool=name,
                    status="failed",
                    output=result,
                    error=decision.reason,
                    metadata={
                        "requires_approval": decision.requires_approval,
                        "status_source": "permission",
                    },
                ),
                tool_context=tool_context,
                agent_name=agent_name,
            )

    try:
        result = _invoke_registered_tool(
            fn,
            arguments,
            runtime_state=runtime_state,
            agent_name=agent_name,
            tool_context=tool_context,
        )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        tool_result = ToolResult(
            tool=name,
            status="failed",
            output=f"[error] {error}",
            error=error,
            metadata={"status_source": "exception"},
        )
    else:
        tool_result = _coerce_tool_result(name, result)

    # Prepend the auto-fix warning so the model knows what was corrected
    if fix_warning:
        tool_result = tool_result.with_output_prefix(fix_warning)

    return _finalize_tool_result(
        tool_result,
        tool_context=tool_context,
        agent_name=agent_name,
    )


def _invoke_registered_tool(
    fn: Callable,
    arguments: dict,
    *,
    runtime_state,
    agent_name: str | None,
    tool_context: ToolContext | None,
):
    kwargs = dict(arguments)
    parameters = inspect.signature(fn).parameters
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    extras = {
        "runtime_state": runtime_state,
        "agent_name": agent_name,
        "tool_context": tool_context,
    }
    for key, value in extras.items():
        if key not in kwargs and (key in parameters or accepts_kwargs):
            kwargs[key] = value
    return fn(**kwargs)


def _finalize_tool_result(
    tool_result: ToolResult,
    *,
    tool_context: ToolContext | None,
    agent_name: str | None,
) -> str:
    result_text = tool_result.to_text()
    if tool_context is not None:
        _emit_structured_tool_result(tool_result, tool_context=tool_context, agent_name=agent_name)
        _emit_file_change_events(tool_result, tool_context=tool_context, agent_name=agent_name)
        payload = {
            "tool": tool_result.tool,
            "status": tool_result.status,
            "ok": tool_result.ok,
            "result": result_text[:500],
        }
        if "requires_approval" in tool_result.metadata:
            payload["requires_approval"] = tool_result.metadata["requires_approval"]
        tool_context.event_bus.emit("after_tool", agent=agent_name, payload=payload)
    return result_text


def _coerce_tool_result(name: str, result) -> ToolResult:
    if isinstance(result, ToolResult):
        return result
    return unstructured_tool_result_from_text(tool=name, text=str(result))


def _emit_structured_tool_result(
    tool_result: ToolResult,
    *,
    tool_context: ToolContext,
    agent_name: str | None,
) -> None:
    tool_context.event_bus.emit_event(
        ToolResultEvent(
            tool=tool_result.tool,
            status=tool_result.status,
            output=tool_result.output,
            error=tool_result.error,
            return_code=tool_result.return_code,
            metadata=tool_result.metadata,
            agent=agent_name,
        ).to_event()
    )
    if tool_result.status == "failed":
        tool_context.event_bus.emit_event(
            FailureEvent(
                category="tool_error",
                message=tool_result.error or tool_result.output,
                agent=agent_name,
            ).to_event()
        )


def _emit_file_change_events(
    tool_result: ToolResult,
    *,
    tool_context: ToolContext,
    agent_name: str | None,
) -> None:
    file_changes = tool_result.metadata.get("file_changes")
    if not isinstance(file_changes, list):
        return
    for change in file_changes:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        if not path:
            continue
        payload = {
            "path": str(path),
            "snapshot_path": change.get("snapshot_path"),
        }
        if change.get("operation"):
            payload["operation"] = change["operation"]
        tool_context.event_bus.emit("file_changed", agent=agent_name, payload=payload)
        tool_context.event_bus.emit_event(
            FileChangeEvent(
                path=str(path),
                operation=change.get("operation"),
                snapshot_path=change.get("snapshot_path"),
                agent=agent_name,
            ).to_event()
        )


def _redact_tool_args(arguments: dict) -> dict:
    redacted = dict(arguments or {})
    if "content" in redacted:
        redacted["content"] = f"[{len(str(redacted['content']))} chars]"
    return redacted
