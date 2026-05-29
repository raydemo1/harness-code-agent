"""Completion logic for slash commands and @mentions (UI-agnostic)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .commands import SlashCommandRegistry


EXCLUDED_DIRS = {
    ".git",
    ".harness",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    ".next",
}


@dataclass(frozen=True)
class MentionCandidate:
    insert_text: str
    display: str
    description: str


def fuzzy_command_candidates(registry: SlashCommandRegistry, query: str):
    """Return slash command specs matching the query, sorted by relevance."""
    query = query.strip()
    scored = [
        (fuzzy_score(query, spec.name), index, spec)
        for index, spec in enumerate(registry.candidates())
    ]
    return [
        spec
        for score, _index, spec in sorted(scored, key=lambda item: (-item[0], item[1]))
        if score > 0
    ]


def current_mention_query(text_before_cursor: str) -> tuple[str, int] | None:
    """Extract the current @mention query from text before cursor."""
    quote_idx = text_before_cursor.rfind('@"')
    plain_idx = text_before_cursor.rfind("@")
    if quote_idx >= 0 and quote_idx + 2 >= plain_idx:
        prefix = text_before_cursor[quote_idx + 2:]
        if '"' in prefix:
            return None
        return prefix, -(len(prefix) + 2)
    if plain_idx == -1:
        return None
    if plain_idx > 0 and not text_before_cursor[plain_idx - 1].isspace():
        return None
    prefix = text_before_cursor[plain_idx + 1:]
    if any(ch.isspace() for ch in prefix):
        return None
    return prefix, -(len(prefix) + 1)


def mention_candidates(root: Path, prefix: str, session_store, *, limit: int = 50) -> list[MentionCandidate]:
    """Return file/session candidates matching the @mention prefix."""
    prefix = prefix.strip()
    if prefix.startswith("session:"):
        session_prefix = prefix.removeprefix("session:")
        candidates = []
        for item in session_store.list_sessions()[:100]:
            session_id = item.get("id", "")
            score = fuzzy_score(session_prefix, session_id)
            if score <= 0 and session_prefix:
                continue
            insert = f"session:{session_id}"
            candidates.append((
                score or 1,
                MentionCandidate(
                    insert_text=insert,
                    display=insert,
                    description=f"{item.get('profile', '')} {item.get('created_at', '')}".strip(),
                ),
            ))
        return [candidate for _score, candidate in sorted(candidates, key=lambda item: -item[0])[:limit]]

    file_candidates = []
    for rel, is_dir in iter_workspace_paths(root, limit=2000):
        score = fuzzy_score(prefix, rel)
        if score <= 0 and prefix:
            continue
        insert = quote_mention_path(rel)
        file_candidates.append((
            score or 1,
            MentionCandidate(
                insert_text=insert,
                display=rel + ("/" if is_dir and not rel.endswith("/") else ""),
                description="directory" if is_dir else "file",
            ),
        ))
    file_candidates.sort(key=lambda item: (-item[0], item[1].display.lower()))
    return [candidate for _score, candidate in file_candidates[:limit]]


def iter_workspace_paths(root: Path, *, limit: int = 2000) -> Iterable[tuple[str, bool]]:
    """Iterate workspace files and directories, excluding common dirs."""
    import os
    root = Path(root)
    results = []

    def walk(curr_dir: Path):
        try:
            for entry in os.scandir(curr_dir):
                if entry.name in EXCLUDED_DIRS:
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                entry_path = Path(entry.path)
                try:
                    rel = entry_path.relative_to(root).as_posix()
                except ValueError:
                    continue
                if not rel:
                    continue
                results.append((rel, is_dir))
                if is_dir:
                    walk(entry_path)
        except OSError:
            pass

    walk(root)
    results.sort(key=lambda item: item[0].lower())
    for rel, is_dir in results[:limit]:
        yield rel, is_dir


def quote_mention_path(path: str) -> str:
    """Quote a path containing spaces for @mention."""
    if any(ch.isspace() for ch in path):
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return path


def fuzzy_score(query: str, candidate: str) -> int:
    """Score how well a candidate matches the query. Higher = better."""
    query = query.lower()
    candidate_lower = candidate.lower()
    if not query:
        return 1
    if candidate_lower.startswith(query):
        return 1000 - len(candidate)
    if query in candidate_lower:
        return 700 - candidate_lower.index(query)
    score = 0
    pos = 0
    streak = 0
    for ch in query:
        idx = candidate_lower.find(ch, pos)
        if idx == -1:
            return 0
        if idx == pos:
            streak += 1
            score += 15 + streak
        else:
            streak = 0
            score += 5
        pos = idx + 1
    return score
