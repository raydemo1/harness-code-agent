from __future__ import annotations

from collections import Counter

from .store import MEMORY_CONTENT_FILES, MemoryRecord, MemoryStore

FILE_PURPOSES = {
    "project.md": "Architecture, modules, conventions, project facts",
    "decisions.md": "Design decisions, tradeoffs, rationale",
    "commands.md": "Useful commands and workflows",
    "debugging.md": "Debug observations, failures, fixes",
    "preferences.md": "User preferences and work style",
    "learnings.md": "Agent experiences and mental summaries",
}


def rebuild_navigation(store: MemoryStore, *, dream_summary: str = "") -> str:
    return rebuild_navigation_from_records(store.read_records(), dream_summary=dream_summary)


def rebuild_navigation_from_records(records: list[MemoryRecord], *, dream_summary: str = "") -> str:
    active = [record for record in records if record.status == "active" and not record.superseded_by]
    counts = Counter(record.file for record in active)
    lines = [
        "# Long-Term Memory",
        "",
        "This is a dynamic user-context navigation surface. It is injected only when useful memory exists.",
        "",
        "## Files",
        "",
        "| File | Purpose | Active records |",
        "|------|---------|----------------|",
    ]
    for filename in MEMORY_CONTENT_FILES:
        lines.append(f"| {filename} | {FILE_PURPOSES[filename]} | {counts.get(filename, 0)} |")
    lines.extend(["", "## Hot Notes", ""])
    recent = sorted(active, key=lambda record: record.updated_at, reverse=True)[:8]
    if not recent:
        lines.append("- No active long-term memory yet.")
    for record in recent:
        lines.append(f"- [{record.id}] {record.title} -> {record.file}#{record.anchor}")
    if dream_summary:
        lines.extend(["", "## Last Dream", "", dream_summary])
    return "\n".join(lines).rstrip() + "\n"
