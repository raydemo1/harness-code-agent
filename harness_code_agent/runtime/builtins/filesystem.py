"""Workspace filesystem tools."""
from __future__ import annotations

from pathlib import Path

from ... import config
from ..tool_context import ToolContext
from ..tool_result import ToolResult


READ_FILE_MAX_LINES = 500
READ_FILE_MAX_OUTPUT_CHARS = 100_000


def _resolve(path: str) -> Path:
    """Resolve a relative path inside the workspace. Prevent escaping."""
    p = Path(config.WORKSPACE, path).resolve()
    ws = Path(config.WORKSPACE).resolve()
    if not str(p).startswith(str(ws)):
        raise ValueError(f"Path escapes workspace: {path}")
    return p


def read_file(
    path: str,
    start_line: int | None = None,
    max_lines: int | None = None,
    include_line_numbers: bool = False,
    tool_context: ToolContext | None = None,
) -> ToolResult:
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
    lines = content.splitlines()
    total_lines = len(lines)
    start = max(1, int(start_line or 1))
    requested_lines = int(max_lines) if max_lines is not None else max(0, total_lines - start + 1)
    if requested_lines > READ_FILE_MAX_LINES:
        return ToolResult(
            tool="read_file",
            status="failed",
            output=(
                f"[error] read_file can read at most {READ_FILE_MAX_LINES} lines per call. "
                f"max_lines must be <= {READ_FILE_MAX_LINES}. "
                f"{path} has {total_lines} total lines; requested {requested_lines} lines. "
                f"Use start_line and max_lines <= {READ_FILE_MAX_LINES} to read a bounded range."
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
    if len(output) > READ_FILE_MAX_OUTPUT_CHARS:
        return ToolResult(
            tool="read_file",
            status="failed",
            output=(
                f"[error] read_file output window is too large ({len(output)} chars). "
                f"Use a smaller max_lines value or a narrower start_line range. "
                f"The per-call output limit is {READ_FILE_MAX_OUTPUT_CHARS} chars."
            ),
            error=f"read_file output window is too large: {len(output)} chars",
            metadata={
                "path": path,
                "start_line": start,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "output_chars": len(output),
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
