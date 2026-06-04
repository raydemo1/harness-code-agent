"""Deferred tool search and progressive disclosure."""
from __future__ import annotations

from ..permissions import VALID_TOOL_PERMISSIONS
from ..tool_context import ToolContext
from ..tool_registry import ToolSpec
from ..tool_result import ToolResult
from ..tool_search import SearchDocument, expand_search_text, search_bm25


def tool_search(query: str, max_results: int = 8, tool_context: ToolContext | None = None) -> ToolResult:
    """Search currently hidden deferred tools and reveal matching schemas."""
    query = str(query or "").strip()
    try:
        max_results = max(1, min(int(max_results or 8), 20))
    except (TypeError, ValueError):
        max_results = 8

    if tool_context is None or tool_context.tool_registry is None:
        return ToolResult(
            tool="tool_search",
            status="failed",
            output="[error] tool_search requires a tool registry in the current tool context.",
            error="tool_search requires tool context",
            metadata={"status_source": "validation"},
        )

    registry = tool_context.tool_registry
    allowed_permissions = (
        set(tool_context.allowed_tool_permissions)
        if tool_context.allowed_tool_permissions is not None
        else set(VALID_TOOL_PERMISSIONS)
    )
    blocked_names = set(tool_context.blocked_tool_names or set())
    already_revealed = set(tool_context.revealed_tool_names or set())

    documents: list[SearchDocument] = []
    for spec in registry.specs():
        if spec.disclosure != "deferred":
            continue
        if spec.name in blocked_names or spec.name in already_revealed:
            continue
        if spec.permission not in allowed_permissions:
            continue
        documents.append(_tool_search_document(spec))

    if not query:
        return ToolResult(
            tool="tool_search",
            status="failed",
            output="[error] tool_search requires a non-empty query.",
            error="tool_search requires query",
            metadata={"status_source": "validation"},
        )
    if not documents:
        return ToolResult(
            tool="tool_search",
            status="success",
            output="No hidden deferred tools are available for the current profile.",
            metadata={"revealed_tool_names": [], "status_source": "native"},
        )

    hits = search_bm25(documents, query, limit=max_results)
    revealed = [hit.key for hit in hits]
    if not hits:
        return ToolResult(
            tool="tool_search",
            status="success",
            output=f"No deferred tools matched query: {query}",
            metadata={"query": query, "revealed_tool_names": [], "status_source": "native"},
        )

    lines = [f"Tool search results for: {query}"]
    for index, hit in enumerate(hits, start=1):
        description = str(hit.metadata.get("description") or "").strip()
        if len(description) > 180:
            description = description[:177] + "..."
        reason = str(hit.metadata.get("match_text") or "").strip()
        if len(reason) > 160:
            reason = reason[:157] + "..."
        lines.append(
            f"{index}. {hit.key} score={hit.score:.3f} revealed=yes"
            + (f"\n   {description}" if description else "")
            + (f"\n   matched: {reason}" if reason else "")
        )

    return ToolResult(
        tool="tool_search",
        status="success",
        output="\n".join(lines),
        metadata={
            "query": query,
            "revealed_tool_names": revealed,
            "status_source": "native",
        },
    )


def _tool_search_document(spec: ToolSpec) -> SearchDocument:
    function = spec.schema.get("function", {}) if isinstance(spec.schema, dict) else {}
    description = str(function.get("description") or "")
    parameters = function.get("parameters") if isinstance(function.get("parameters"), dict) else {}
    parameter_text = _schema_parameter_text(parameters)
    server_tool_text = ""
    if spec.name.startswith("mcp__"):
        server_tool_text = spec.name.removeprefix("mcp__").replace("__", " ")
    text = "\n".join(
        part
        for part in [
            expand_search_text(spec.name),
            expand_search_text(server_tool_text),
            description,
            parameter_text,
            spec.permission,
        ]
        if part
    )
    match_text = " ".join(
        part for part in [expand_search_text(spec.name), description, parameter_text] if part
    )
    return SearchDocument(
        key=spec.name,
        text=text,
        metadata={
            "description": description,
            "permission": spec.permission,
            "match_text": match_text,
        },
    )


def _schema_parameter_text(schema: dict) -> str:
    parts: list[str] = []
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, raw in properties.items():
            parts.append(expand_search_text(str(name)))
            if isinstance(raw, dict):
                description = raw.get("description")
                if description:
                    parts.append(str(description))
                nested = raw.get("items") if isinstance(raw.get("items"), dict) else raw
                if isinstance(nested, dict) and nested is not raw:
                    parts.append(_schema_parameter_text(nested))
    return " ".join(part for part in parts if part)
