from __future__ import annotations

import time

from ...memory.dream import run_dream, should_dream
from ...memory.recall import MemoryRecall
from ...memory.store import MEMORY_CONTENT_FILES, PROTECTED_FILES, MemoryStore, default_memory_root
from ..tool_context import ToolContext
from ..tool_result import ToolResult


def memory_search(query: str, tool_context: ToolContext | None = None) -> ToolResult:
    if not query or not query.strip():
        return ToolResult(
            tool="memory_search",
            status="failed",
            output="[error] query is required",
            error="query is required",
            metadata={"status_source": "validation"},
        )
    workspace = tool_context.workspace.root if tool_context is not None else None
    if workspace is None:
        return ToolResult(
            tool="memory_search",
            status="failed",
            output="[error] memory_search requires a workspace tool context",
            error="missing tool context",
            metadata={"status_source": "validation"},
        )
    store = MemoryStore(default_memory_root(workspace), workspace=workspace)
    if not store.exists() or not store.has_active_records():
        return ToolResult(
            tool="memory_search",
            status="success",
            output="No relevant memory found.",
            metadata={"status_source": "native", "memory_root": str(store.root), "result_count": 0},
        )
    hits = MemoryRecall(store).search(query)
    if not hits:
        return ToolResult(
            tool="memory_search",
            status="success",
            output="No relevant memory found.",
            metadata={"status_source": "native", "memory_root": str(store.root), "result_count": 0},
        )
    lines = [f"Found {len(hits)} memory entries:"]
    for hit in hits:
        record = hit.record
        lines.append(f"- [{record.id}] {record.title} ({record.file}#{record.anchor}, score={hit.score:.2f})")
        lines.append(f"  Summary: {record.summary}")
        if record.tags:
            lines.append(f"  Tags: {', '.join(record.tags[:6])}")
        if record.source_paths:
            lines.append(f"  Source paths: {', '.join(record.source_paths[:4])}")
    lines.append("")
    lines.append("Use read_memory_file to read full details from the memory files.")
    return ToolResult(
        tool="memory_search",
        status="success",
        output="\n".join(lines),
        metadata={"status_source": "native", "memory_root": str(store.root), "result_count": len(hits)},
    )


def remember_memory(
    summary: str,
    title: str = "",
    file: str = "",
    tags: list[str] | None = None,
    source_paths: list[str] | None = None,
    confidence: float = 0.7,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    if not summary or not summary.strip():
        return ToolResult(
            tool="remember_memory",
            status="failed",
            output="[error] summary is required",
            error="summary is required",
            metadata={"status_source": "validation"},
        )
    if file in PROTECTED_FILES:
        return ToolResult(
            tool="remember_memory",
            status="failed",
            output=f"[error] {file} is managed by Dream and cannot be written directly",
            error=f"protected memory file: {file}",
            metadata={"status_source": "validation", "file": file},
        )
    if file and file not in MEMORY_CONTENT_FILES:
        return ToolResult(
            tool="remember_memory",
            status="failed",
            output=f"[error] Unknown memory file: {file}",
            error=f"unknown memory file: {file}",
            metadata={"status_source": "validation", "file": file},
        )
    workspace = tool_context.workspace.root if tool_context is not None else None
    if workspace is None:
        return ToolResult(
            tool="remember_memory",
            status="failed",
            output="[error] remember_memory requires a workspace tool context",
            error="missing tool context",
            metadata={"status_source": "validation"},
        )
    store = MemoryStore(default_memory_root(workspace), workspace=workspace)
    candidate = {
        "title": title.strip() or summary.strip()[:80],
        "summary": summary.strip(),
        "file": file.strip(),
        "tags": tags or [],
        "source_paths": source_paths or [],
        "source_sessions": [tool_context.session_id] if tool_context and tool_context.session_id else [],
        "confidence": confidence,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    store.append_candidate(candidate)
    dream_summary = ""
    if should_dream(store):
        dream_summary = run_dream(store)
    output = "Queued memory candidate for Dream merge."
    if dream_summary:
        output += f"\n{dream_summary}"
    return ToolResult(
        tool="remember_memory",
        status="success",
        output=output,
        metadata={"status_source": "native", "memory_root": str(store.root), "dream": dream_summary},
    )


def read_memory_file(file: str = "MEMORY.md", tool_context: ToolContext | None = None) -> ToolResult:
    if file not in ("MEMORY.md", *MEMORY_CONTENT_FILES, "dream-log.md"):
        return ToolResult(
            tool="read_memory_file",
            status="failed",
            output=f"[error] Unknown readable memory file: {file}",
            error=f"unknown memory file: {file}",
            metadata={"status_source": "validation", "file": file},
        )
    workspace = tool_context.workspace.root if tool_context is not None else None
    if workspace is None:
        return ToolResult(
            tool="read_memory_file",
            status="failed",
            output="[error] read_memory_file requires a workspace tool context",
            error="missing tool context",
            metadata={"status_source": "validation"},
        )
    store = MemoryStore(default_memory_root(workspace), workspace=workspace)
    if not store.exists():
        return ToolResult(
            tool="read_memory_file",
            status="success",
            output="(no memory files yet)",
            metadata={"status_source": "native", "memory_root": str(store.root), "file": file},
        )
    content = store.read_memory_file(file)
    return ToolResult(
        tool="read_memory_file",
        status="success",
        output=content or "(empty)",
        metadata={"status_source": "native", "memory_root": str(store.root), "file": file},
    )
