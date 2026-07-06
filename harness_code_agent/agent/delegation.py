"""Delegated sub-agent tool."""
from __future__ import annotations

import difflib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.builtins.schemas import CORE_TOOL_SCHEMAS
from ..runtime.middleware import AgentMiddleware
from ..runtime.permissions import PermissionPolicy, is_read_only_command
from ..runtime.tool_context import ToolContext
from ..runtime.tool_registry import ToolRegistry
from ..runtime.tool_result import ToolResult
from ..sessions.events import EventBus
from ..workspace.service import WorkspaceService


DELEGATE_AGENT_PROFILES = {"explore", "test_design", "review", "verify", "patch"}
READ_ONLY_DELEGATE_PROFILES = {"explore", "test_design", "review", "verify"}
PATCH_DELEGATE_PROFILE = "patch"
DELEGATE_MAX_SECONDS_DEFAULT = 300
DELEGATE_MAX_TURNS_DEFAULT = 6
DELEGATE_MAX_OUTPUT_CHARS = 12_000
DELEGATE_MAX_DIFF_CHARS = 30_000

_COPY_EXCLUDED_NAMES = {
    ".git",
    ".harness",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "node_modules",
    "jobs",
}
_COPY_EXCLUDED_PATHS = {("eval", "results")}
_TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class DelegateSpec:
    name: str
    mode: str
    tool_names: tuple[str, ...]
    prompt: str
    read_only_shell: bool = True


class DelegatePolicyMiddleware(AgentMiddleware):
    """Constrain delegated agents to their declared role."""

    _CONTROL_TOOLS = {
        "ask_user",
        "update_plan_state",
        "list_shell_jobs",
        "read_shell_output",
        "stop_shell_job",
        "browser_test",
        "stop_dev_server",
        "delegate_agent",
        "parallel_agents",
        "parallel_commands",
    }
    _WRITE_TOOLS = {"write_file", "apply_patch", "remember_memory"}

    def __init__(self, spec: DelegateSpec, *, allowed_paths: list[str] | None = None):
        self.spec = spec
        self.allowed_paths = [str(path).strip().replace("\\", "/") for path in allowed_paths or [] if str(path).strip()]

    def before_tool(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> ToolResult | None:
        if tool_name in self._CONTROL_TOOLS:
            return _blocked(tool_name, "delegated agents cannot control workflow state, ask the user, manage jobs, or delegate again")
        if self.spec.mode == "read_only" and tool_name in self._WRITE_TOOLS:
            return _blocked(tool_name, "this delegated agent is read-only")
        if tool_name == "run_bash" and self.spec.read_only_shell:
            command = str(tool_args.get("command") or "")
            if not is_read_only_command(command):
                return _blocked(tool_name, "delegated agents may only run read-only or verification shell commands")
        if self.allowed_paths and tool_name in {"read_file", "write_file", "apply_patch", "repo_search", "list_files"}:
            path = _tool_path(tool_name, tool_args)
            if path and not _path_allowed(path, self.allowed_paths):
                return _blocked(tool_name, f"path is outside allowed_paths: {path}")
        return None


def delegate_agent(
    agent_profile: str,
    task: str,
    expected_output: str = "",
    allowed_paths: list[str] | None = None,
    max_turns: int = DELEGATE_MAX_TURNS_DEFAULT,
    max_seconds: int = DELEGATE_MAX_SECONDS_DEFAULT,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    """Delegate bounded work to a focused sub-agent."""
    profile = str(agent_profile or "").strip().lower().replace("-", "_")
    if profile not in DELEGATE_AGENT_PROFILES:
        output = "[error] Invalid agent_profile. Use one of: " + ", ".join(sorted(DELEGATE_AGENT_PROFILES))
        return ToolResult(tool="delegate_agent", status="failed", output=output, error=output.removeprefix("[error] "), metadata={"status_source": "validation"})
    task = str(task or "").strip()
    if not task:
        return ToolResult(tool="delegate_agent", status="failed", output="[error] delegate_agent requires a non-empty task", error="task is required", metadata={"status_source": "validation"})
    max_seconds = _clamp_int(max_seconds, default=DELEGATE_MAX_SECONDS_DEFAULT, minimum=30, maximum=1800)
    max_turns = _clamp_int(max_turns, default=DELEGATE_MAX_TURNS_DEFAULT, minimum=1, maximum=20)
    spec = _delegate_spec(profile)
    workspace_root = _workspace_root(tool_context)

    if spec.mode == "isolated_patch":
        return _run_patch_delegate(
            spec,
            task=task,
            expected_output=expected_output,
            allowed_paths=allowed_paths or [],
            max_turns=max_turns,
            max_seconds=max_seconds,
            workspace_root=workspace_root,
            parent_context=tool_context,
        )
    return _run_read_only_delegate(
        spec,
        task=task,
        expected_output=expected_output,
        allowed_paths=allowed_paths or [],
        max_turns=max_turns,
        max_seconds=max_seconds,
        workspace_root=workspace_root,
        parent_context=tool_context,
    )


def delegate_tool_schemas_for_profile(profile: str) -> list[dict]:
    spec = _delegate_spec(profile)
    return [_schema_by_name(name) for name in spec.tool_names if _schema_by_name(name) is not None]


def _run_read_only_delegate(
    spec: DelegateSpec,
    *,
    task: str,
    expected_output: str,
    allowed_paths: list[str],
    max_turns: int,
    max_seconds: int,
    workspace_root: Path,
    parent_context: ToolContext | None,
) -> ToolResult:
    raw = _run_delegate_loop(
        spec,
        task=task,
        expected_output=expected_output,
        allowed_paths=allowed_paths,
        max_turns=max_turns,
        max_seconds=max_seconds,
        workspace_root=workspace_root,
        parent_context=parent_context,
    )
    report = _coerce_delegate_report(spec, raw, mode=spec.mode)
    return ToolResult(
        tool="delegate_agent",
        status="success" if report.get("status") == "completed" else "failed",
        output=_json_limited(report, DELEGATE_MAX_OUTPUT_CHARS),
        error=None if report.get("status") == "completed" else str(report.get("summary") or "delegated agent did not complete"),
        metadata={"agent_profile": spec.name, "mode": spec.mode, "status_source": "native"},
    )


def _run_patch_delegate(
    spec: DelegateSpec,
    *,
    task: str,
    expected_output: str,
    allowed_paths: list[str],
    max_turns: int,
    max_seconds: int,
    workspace_root: Path,
    parent_context: ToolContext | None,
) -> ToolResult:
    start = time.time()
    with tempfile.TemporaryDirectory(prefix="hca-delegate-") as temp_dir:
        temp_root = Path(temp_dir) / "workspace"
        _copy_workspace(workspace_root, temp_root)
        raw = _run_delegate_loop(
            spec,
            task=task,
            expected_output=expected_output,
            allowed_paths=allowed_paths,
            max_turns=max_turns,
            max_seconds=max(30, int(max_seconds - (time.time() - start))),
            workspace_root=temp_root,
            parent_context=parent_context,
        )
        diff, changed_files = _workspace_diff(workspace_root, temp_root)

    report = _coerce_delegate_report(spec, raw, mode=spec.mode)
    report["proposed_patch"] = diff[:DELEGATE_MAX_DIFF_CHARS]
    if len(diff) > DELEGATE_MAX_DIFF_CHARS:
        report["proposed_patch"] += f"\n[truncated] proposed_patch exceeded {DELEGATE_MAX_DIFF_CHARS} chars."
    report["changed_files"] = changed_files
    status = "success" if report.get("status") == "completed" else "failed"
    return ToolResult(
        tool="delegate_agent",
        status=status,
        output=_json_limited(report, DELEGATE_MAX_OUTPUT_CHARS + DELEGATE_MAX_DIFF_CHARS),
        error=None if status == "success" else str(report.get("summary") or "patch delegate did not complete"),
        metadata={
            "agent_profile": spec.name,
            "mode": spec.mode,
            "changed_files": changed_files,
            "status_source": "native",
        },
    )


def _run_delegate_loop(
    spec: DelegateSpec,
    *,
    task: str,
    expected_output: str,
    allowed_paths: list[str],
    max_turns: int,
    max_seconds: int,
    workspace_root: Path,
    parent_context: ToolContext | None,
) -> str:
    from .conversation import Agent

    workspace = WorkspaceService(root=workspace_root)
    registry = _delegate_registry(spec)
    event_bus = parent_context.event_bus if parent_context is not None else EventBus()
    context = ToolContext(
        workspace=workspace,
        permission_policy=PermissionPolicy(mode="danger-full-access"),
        event_bus=event_bus,
        session_id=(parent_context.session_id if parent_context is not None else None),
        tool_registry=registry,
        allowed_tool_permissions={"read", "network_read", "edit", "shell"},
        blocked_tool_names=set(),
    )
    prompt = _delegate_prompt(spec, max_turns=max_turns)
    sub = Agent(
        name=f"delegate_{spec.name}",
        system_prompt=prompt,
        use_tools=True,
        tool_schemas=delegate_tool_schemas_for_profile(spec.name),
        middlewares=[DelegatePolicyMiddleware(spec, allowed_paths=allowed_paths)],
        time_budget=float(max_seconds),
        tool_context=context,
    )
    return sub.run(_delegate_task(task, expected_output=expected_output, allowed_paths=allowed_paths))


def _delegate_registry(spec: DelegateSpec) -> ToolRegistry:
    from ..runtime.builtins.registry import BUILTIN_TOOL_REGISTRY

    registry = ToolRegistry()
    source = BUILTIN_TOOL_REGISTRY
    for tool_name in spec.tool_names:
        handler = source.get(tool_name)
        schema = _schema_by_name(tool_name)
        permission = source.permission_for(tool_name)
        lane = source.lane_for(tool_name)
        if handler is not None and schema is not None and permission is not None:
            registry.register(schema, handler, permission=permission, lane=lane)
    return registry


def _delegate_spec(profile: str) -> DelegateSpec:
    prompts = {
        "explore": (
            "Investigate the codebase independently. Focus on facts, file paths, symbols, command evidence, "
            "and unresolved risks. Do not propose edits unless asked for recommendations."
        ),
        "test_design": (
            "Design tests and verification commands. Provide cases, assertions, fixtures, and failure modes. "
            "Do not write tests or modify files."
        ),
        "review": (
            "Review independently for bugs, regressions, missing tests, security risks, and maintainability. "
            "Prioritize findings by severity and cite evidence."
        ),
        "verify": (
            "Run focused read-only or verification commands and interpret their output. Do not modify files."
        ),
        "patch": (
            "Work in an isolated copy of the workspace. You may edit files in that copy and run verification. "
            "Return a concise summary; the main agent will review and decide whether to apply the patch."
        ),
    }
    if profile == "patch":
        tools = ("read_file", "list_files", "repo_search", "write_file", "apply_patch", "run_bash", "web_search", "web_fetch")
        return DelegateSpec(profile, "isolated_patch", tools, prompts[profile], read_only_shell=True)
    tools = ("read_file", "list_files", "repo_search", "run_bash", "web_search", "web_fetch")
    return DelegateSpec(profile, "read_only", tools, prompts[profile], read_only_shell=True)


def _delegate_prompt(spec: DelegateSpec, *, max_turns: int) -> str:
    return (
        f"You are a delegated {spec.name} agent. {spec.prompt}\n"
        "You do not own the overall task, final integration, final verification, or the stop decision.\n"
        f"Use at most about {max_turns} reasoning/tool rounds. Stay tightly scoped to the delegated task.\n"
        "Return only JSON with this shape:\n"
        "{\n"
        '  "status": "completed | blocked | failed",\n'
        f'  "agent": "{spec.name}",\n'
        f'  "mode": "{spec.mode}",\n'
        '  "summary": "...",\n'
        '  "findings": ["..."],\n'
        '  "evidence": ["file/path.py:line or command output summary"],\n'
        '  "recommendations": ["..."],\n'
        '  "risks": ["..."],\n'
        '  "verification": ["..."]\n'
        "}\n"
    )


def _delegate_task(task: str, *, expected_output: str, allowed_paths: list[str]) -> str:
    parts = [f"Delegated task:\n{task.strip()}"]
    if expected_output:
        parts.append(f"Expected output focus:\n{str(expected_output).strip()}")
    if allowed_paths:
        parts.append("Allowed paths:\n" + "\n".join(f"- {path}" for path in allowed_paths))
    return "\n\n".join(parts)


def _coerce_delegate_report(spec: DelegateSpec, raw_result: str, *, mode: str) -> dict[str, Any]:
    raw_result = raw_result or ""
    try:
        parsed = json.loads(raw_result)
        if isinstance(parsed, dict):
            report = parsed
        else:
            raise ValueError("not an object")
    except Exception:
        report = {
            "status": "completed" if raw_result.strip() else "blocked",
            "agent": spec.name,
            "mode": mode,
            "summary": raw_result[:4000] if raw_result.strip() else "delegated agent returned no content",
            "findings": [raw_result[:7000]] if raw_result.strip() else [],
            "evidence": [],
            "recommendations": [],
            "risks": [],
            "verification": [],
        }
    report.setdefault("status", "completed")
    report.setdefault("agent", spec.name)
    report.setdefault("mode", mode)
    for key in ("findings", "evidence", "recommendations", "risks", "verification"):
        value = report.get(key)
        if not isinstance(value, list):
            report[key] = [str(value)] if value else []
    report.setdefault("summary", "")
    report.setdefault("proposed_patch", "")
    report.setdefault("changed_files", [])
    return report


def _schema_by_name(name: str) -> dict | None:
    for schema in CORE_TOOL_SCHEMAS:
        if schema.get("function", {}).get("name") == name:
            return schema
    return None


def _workspace_root(tool_context: ToolContext | None = None) -> Path:
    if tool_context is not None:
        return tool_context.workspace.root.resolve()
    from .. import config

    return Path(config.WORKSPACE).resolve()


def _copy_workspace(source: Path, dest: Path) -> None:
    source = source.resolve()

    def ignore(path: str, names: list[str]) -> set[str]:
        current = Path(path).resolve()
        ignored: set[str] = set()
        for name in names:
            item = current / name
            try:
                rel = item.resolve().relative_to(source)
            except ValueError:
                ignored.add(name)
                continue
            if _excluded_rel(rel):
                ignored.add(name)
        return ignored

    shutil.copytree(source, dest, ignore=ignore)


def _workspace_diff(original: Path, modified: Path) -> tuple[str, list[str]]:
    original_files = _tracked_textish_files(original)
    modified_files = _tracked_textish_files(modified)
    all_rel = sorted(set(original_files) | set(modified_files))
    sections: list[str] = []
    changed: list[str] = []
    for rel in all_rel:
        before_path = original / rel
        after_path = modified / rel
        before = _read_text_lines(before_path) if before_path.exists() else []
        after = _read_text_lines(after_path) if after_path.exists() else []
        if before == after:
            continue
        changed.append(rel.as_posix())
        sections.extend(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{rel.as_posix()}",
                tofile=f"b/{rel.as_posix()}",
                lineterm="",
            )
        )
    return "\n".join(sections), changed


def _tracked_textish_files(root: Path) -> set[Path]:
    files: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _excluded_rel(rel) or not _textish(path):
            continue
        files.add(rel)
    return files


def _excluded_rel(rel: Path) -> bool:
    parts = rel.parts
    if any(part in _COPY_EXCLUDED_NAMES for part in parts):
        return True
    lower_parts = tuple(part.lower() for part in parts)
    return any(_contains_path_parts(lower_parts, blocked) for blocked in _COPY_EXCLUDED_PATHS)


def _contains_path_parts(parts: tuple[str, ...], blocked: tuple[str, ...]) -> bool:
    if len(parts) < len(blocked):
        return False
    blocked_lower = tuple(part.lower() for part in blocked)
    return any(parts[index:index + len(blocked_lower)] == blocked_lower for index in range(len(parts) - len(blocked_lower) + 1))


def _textish(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile", "README"}


def _read_text_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _json_limited(payload: dict[str, Any], limit: int) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= limit:
        return text
    clipped = dict(payload)
    clipped["findings"] = ["\n".join(str(item) for item in payload.get("findings", []))[: max(1000, limit // 2)] + "\n...(truncated)"]
    if clipped.get("proposed_patch"):
        clipped["proposed_patch"] = str(clipped["proposed_patch"])[: max(1000, limit // 2)] + "\n...(truncated)"
    return json.dumps(clipped, ensure_ascii=False)


def _blocked(tool_name: str, reason: str) -> ToolResult:
    return ToolResult(
        tool=tool_name,
        status="failed",
        output=f"[blocked] {reason}.",
        error=reason,
        metadata={"status_source": "delegate_policy"},
    )


def _tool_path(tool_name: str, tool_args: dict) -> str:
    if tool_name == "list_files":
        return str(tool_args.get("directory") or ".")
    return str(tool_args.get("path") or ".")


def _path_allowed(path: str, allowed_paths: list[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(normalized == allowed.strip("./") or normalized.startswith(allowed.strip("./").rstrip("/") + "/") for allowed in allowed_paths)


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
