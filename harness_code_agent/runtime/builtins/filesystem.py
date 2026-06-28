"""Workspace filesystem tools."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ... import config
from ...agent.context import count_text_tokens
from ..tool_context import ToolContext
from ..tool_result import ToolResult


# Dual limit: lines and tokens, whichever is smaller wins.
# Aligned with Claude Code (2000 lines / 100K tokens per tool result).
READ_FILE_MAX_LINES = 2000
READ_FILE_MAX_OUTPUT_TOKENS = 100_000
REPO_SEARCH_TIMEOUT_SECONDS = 15
REPO_SEARCH_MAX_RESULTS = 500
LIST_FILES_MAX_RESULTS = 1000
DEFAULT_EXCLUDE_PARTS = {
    ".git",
    ".harness",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
}
DEFAULT_EXCLUDE_PATHS = {
    ("eval", "results"),
}


def _resolve(path: str) -> Path:
    """Resolve a relative path inside the workspace. Prevent escaping."""
    p = Path(config.WORKSPACE, path).resolve()
    ws = Path(config.WORKSPACE).resolve()
    try:
        p.relative_to(ws)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace: {path}") from exc
    return p


def _workspace_root(tool_context: ToolContext | None = None) -> Path:
    if tool_context is not None:
        return tool_context.workspace.root.resolve()
    return Path(config.WORKSPACE).resolve()


def _resolve_with_context(path: str, tool_context: ToolContext | None = None) -> Path:
    if tool_context is not None:
        return tool_context.workspace.resolve(path)
    return _resolve(path)


def _relative_to_workspace(path: Path, workspace: Path) -> str:
    rel = path.resolve().relative_to(workspace.resolve())
    return "." if str(rel) == "." else rel.as_posix()


def _has_excluded_part(path: Path, workspace: Path, extra_exclude: set[str] | None = None) -> bool:
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return True
    parts = rel.parts
    excluded = DEFAULT_EXCLUDE_PARTS | (extra_exclude or set())
    if any(part in excluded for part in parts):
        return True
    lower_parts = tuple(part.lower() for part in parts)
    return any(_contains_path_parts(lower_parts, blocked) for blocked in DEFAULT_EXCLUDE_PATHS)


def _contains_path_parts(parts: tuple[str, ...], blocked: tuple[str, ...]) -> bool:
    if not blocked or len(parts) < len(blocked):
        return False
    blocked_lower = tuple(part.lower() for part in blocked)
    return any(parts[index:index + len(blocked_lower)] == blocked_lower for index in range(len(parts) - len(blocked_lower) + 1))


def _rg_exclude_globs() -> list[str]:
    globs = [f"!**/{name}/**" for name in sorted(DEFAULT_EXCLUDE_PARTS)]
    globs.append("!eval/results/**")
    globs.append("!**/eval/results/**")
    return globs


def _clamp_int(value: int | None, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _observation_path_blocked(path: Path, workspace: Path) -> bool:
    if os.environ.get("HARNESS_ALLOW_OBSERVATION_READ", "").strip() in {"1", "true", "yes"}:
        return False
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return True
    return ".harness" in rel.parts and "observations" in rel.parts


def repo_search(
    pattern: str,
    path: str = ".",
    glob: list[str] | None = None,
    case_sensitive: bool = False,
    max_results: int = 100,
    context_lines: int = 0,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    workspace = _workspace_root(tool_context)
    p = _resolve_with_context(path or ".", tool_context)
    if not p.exists():
        return ToolResult(
            tool="repo_search",
            status="failed",
            output=f"[error] Search path not found: {path}",
            error=f"Search path not found: {path}",
            metadata={"path": path, "status_source": "native"},
        )
    if _has_excluded_part(p, workspace):
        return ToolResult(
            tool="repo_search",
            status="failed",
            output=f"[blocked] repo_search does not search internal/generated paths by default: {path}",
            error=f"repo_search blocked for excluded path: {path}",
            metadata={"path": path, "status_source": "permission", "reason": "excluded_path"},
        )
    limit = _clamp_int(max_results, default=100, minimum=1, maximum=REPO_SEARCH_MAX_RESULTS)
    context = _clamp_int(context_lines, default=0, minimum=0, maximum=5)
    search_path = _relative_to_workspace(p, workspace)
    args = [
        "rg",
        "--line-number",
        "--with-filename",
        "--color",
        "never",
    ]
    if not case_sensitive:
        args.append("--ignore-case")
    if context:
        args.extend(["--context", str(context)])
    for item in _rg_exclude_globs():
        args.extend(["--glob", item])
    for item in glob or []:
        if item:
            args.extend(["--glob", str(item)])
    args.extend(["--", pattern, search_path])

    try:
        completed = subprocess.run(
            args,
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=REPO_SEARCH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return ToolResult(
            tool="repo_search",
            status="failed",
            output="[error] ripgrep (rg) is not installed or not on PATH.",
            error="ripgrep (rg) is not installed or not on PATH.",
            metadata={"path": path, "status_source": "native"},
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            tool="repo_search",
            status="failed",
            output=f"[error] repo_search timed out after {REPO_SEARCH_TIMEOUT_SECONDS}s. Narrow the path or pattern.",
            error=f"repo_search timed out after {REPO_SEARCH_TIMEOUT_SECONDS}s",
            metadata={"path": path, "status_source": "timeout"},
        )

    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    if completed.returncode not in {0, 1}:
        output = f"[error] rg exited with code {completed.returncode}"
        if stderr:
            output += f"\n{stderr}"
        return ToolResult(
            tool="repo_search",
            status="failed",
            output=output,
            error=output.removeprefix("[error] "),
            metadata={"path": path, "status_source": "native", "returncode": completed.returncode},
        )

    lines = stdout.splitlines()
    truncated = len(lines) > limit
    selected = lines[:limit]
    if not selected:
        output = "(no matches)"
    else:
        output = "\n".join(selected)
        if truncated:
            output += f"\n[truncated] Showing first {limit} result lines. Narrow path/pattern or raise max_results."
    return ToolResult(
        tool="repo_search",
        status="success",
        output=output,
        metadata={
            "path": path,
            "explicit_path": search_path,
            "match_lines": len(lines),
            "returned_lines": len(selected),
            "truncated": truncated,
            "status_source": "native",
        },
    )


def read_file(
    path: str,
    start_line: int | None = None,
    max_lines: int | None = None,
    include_line_numbers: bool = False,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    p = _resolve_with_context(path, tool_context)
    workspace = _workspace_root(tool_context)
    if _observation_path_blocked(p, workspace):
        return ToolResult(
            tool="read_file",
            status="failed",
            output=(
                "[blocked] read_file cannot read raw .harness/observations artifacts during normal runs. "
                "Use the summarized tool result in the conversation, or set HARNESS_ALLOW_OBSERVATION_READ=1 for diagnosis."
            ),
            error="read_file blocked for .harness/observations",
            metadata={"path": path, "status_source": "permission", "reason": "internal_observation_artifact"},
        )
    if not p.exists():
        return ToolResult(
            tool="read_file",
            status="failed",
            output=f"[error] File not found: {path}",
            error=f"File not found: {path}",
            metadata={"path": path, "status_source": "native"},
        )
    content = p.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    total_lines = len(lines)
    try:
        start = max(1, int(start_line or 1))
    except (TypeError, ValueError):
        return ToolResult(
            tool="read_file",
            status="failed",
            output="[error] read_file start_line must be an integer >= 1.",
            error="read_file start_line must be an integer >= 1",
            metadata={
                "path": path,
                "start_line": start_line,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "status_source": "validation",
            },
        )
    try:
        requested_lines = int(max_lines) if max_lines is not None else max(0, total_lines - start + 1)
    except (TypeError, ValueError):
        return ToolResult(
            tool="read_file",
            status="failed",
            output="[error] read_file max_lines must be an integer >= 1.",
            error="read_file max_lines must be an integer >= 1",
            metadata={
                "path": path,
                "start_line": start,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "status_source": "validation",
            },
        )
    if requested_lines < 1:
        return ToolResult(
            tool="read_file",
            status="failed",
            output="[error] read_file max_lines must be an integer >= 1.",
            error="read_file max_lines must be an integer >= 1",
            metadata={
                "path": path,
                "start_line": start,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "status_source": "validation",
            },
        )
    if requested_lines > READ_FILE_MAX_LINES:
        return ToolResult(
            tool="read_file",
            status="failed",
            output=(
                f"[error] read_file can read at most {READ_FILE_MAX_LINES} lines per call. "
                f"max_lines must be <= {READ_FILE_MAX_LINES}. "
                f"{path} has {total_lines} total lines; requested {requested_lines} lines. "
                f"For sequential scans of large files, use a larger window up to {READ_FILE_MAX_LINES} "
                f"lines, then advance start_line by the number of lines read. Use smaller windows when "
                f"following search hits, inspecting local context, or avoiding the per-call token cap "
                f"on dense files."
            ),
            error=f"read_file max_lines must be <= {READ_FILE_MAX_LINES}",
            metadata={
                "path": path,
                "start_line": start,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "status_source": "validation",
            },
        )

    end = len(lines) if max_lines is None else min(len(lines), start + max(0, requested_lines) - 1)
    selected = lines[start - 1:end]
    if include_line_numbers:
        selected = [f"{line_no}: {line}" for line_no, line in enumerate(selected, start=start)]
    output = "\n".join(selected)
    output_tokens = count_text_tokens(output)
    if output_tokens > READ_FILE_MAX_OUTPUT_TOKENS:
        return ToolResult(
            tool="read_file",
            status="failed",
            output=(
                f"[error] read_file output window is too large ({output_tokens} tokens). "
                f"Use a smaller max_lines value or a narrower start_line range. "
                f"The per-call output limit is {READ_FILE_MAX_OUTPUT_TOKENS} tokens "
                f"(or {READ_FILE_MAX_LINES} lines, whichever is smaller)."
            ),
            error=f"read_file output window is too large: {output_tokens} tokens",
            metadata={
                "path": path,
                "start_line": start,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "output_tokens": output_tokens,
                "status_source": "validation",
            },
        )

    return ToolResult(
        tool="read_file",
        status="success",
        output=output,
        metadata={
            "path": path,
            "start_line": start,
            "max_lines": max_lines,
            "total_lines": total_lines,
            "status_source": "native",
        },
    )


def read_skill_file(path: str) -> ToolResult:
    """Read a file from the skills directory (outside workspace). Path must be relative to project root."""
    project_root = Path(__file__).resolve().parents[3]
    p = (project_root / path).resolve()
    # Must stay within the skills directory
    skills_dir = (project_root / "skills").resolve()
    try:
        p.relative_to(skills_dir)
    except ValueError:
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


def list_files(
    directory: str = ".",
    depth: int = 2,
    max_results: int = 200,
    include_hidden: bool = False,
    exclude: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    p = _resolve_with_context(directory or ".", tool_context)
    workspace = _workspace_root(tool_context)
    if not p.is_dir():
        return ToolResult(
            tool="list_files",
            status="failed",
            output=f"[error] Not a directory: {directory}",
            error=f"Not a directory: {directory}",
            metadata={"directory": directory, "status_source": "native"},
        )
    limit = _clamp_int(max_results, default=200, minimum=1, maximum=LIST_FILES_MAX_RESULTS)
    max_depth = _clamp_int(depth, default=2, minimum=1, maximum=20)
    extra_exclude = {str(item) for item in (exclude or []) if str(item).strip()}
    entries: list[str] = []
    total_seen = 0

    def should_skip(item: Path) -> bool:
        if not include_hidden and any(part.startswith(".") for part in item.relative_to(workspace).parts):
            return True
        return _has_excluded_part(item, workspace, extra_exclude)

    for root, dirnames, filenames in os.walk(p):
        root_path = Path(root)
        current_depth = len(root_path.relative_to(p).parts)
        include_children = current_depth + 1 <= max_depth
        descend = current_depth + 1 < max_depth
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if descend and not should_skip(root_path / name)
        ]
        if not include_children:
            dirnames[:] = []
            continue
        for dirname in dirnames:
            item = root_path / dirname
            total_seen += 1
            if len(entries) < limit:
                entries.append(_relative_to_workspace(item, workspace) + "/")
        for filename in sorted(filenames):
            item = root_path / filename
            if should_skip(item):
                continue
            total_seen += 1
            if len(entries) < limit:
                entries.append(_relative_to_workspace(item, workspace))
    if not entries:
        return ToolResult(
            tool="list_files",
            status="success",
            output="(empty)",
            metadata={
                "directory": directory,
                "depth": max_depth,
                "status_source": "native",
                "count": total_seen,
                "returned": 0,
                "truncated": False,
            },
        )
    truncated = total_seen > len(entries)
    output = "\n".join(entries)
    if truncated:
        output += f"\n[truncated] Showing first {len(entries)} entries. Narrow directory or increase max_results."
    return ToolResult(
        tool="list_files",
        status="success",
        output=output,
        metadata={
            "directory": directory,
            "depth": max_depth,
            "status_source": "native",
            "count": total_seen,
            "returned": len(entries),
            "truncated": truncated,
        },
    )
