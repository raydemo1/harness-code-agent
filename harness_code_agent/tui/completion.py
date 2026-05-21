from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

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


class HcaCompleter(Completer):
    def __init__(self, *, registry: SlashCommandRegistry, session):
        self.registry = registry
        self.session = session

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        stripped = text.lstrip()
        if stripped.startswith("/"):
            token = stripped.split(maxsplit=1)[0]
            for spec in fuzzy_command_candidates(self.registry, token):
                yield Completion(
                    spec.name,
                    start_position=-len(token),
                    display=spec.usage,
                    display_meta=f"{spec.group} - {spec.description}",
                )
            return

        mention = current_mention_query(text)
        if mention is None:
            return
        prefix, start_position = mention
        for candidate in mention_candidates(self.session.cwd, prefix, self.session.session_store):
            yield Completion(
                "@" + candidate.insert_text,
                start_position=start_position,
                display=candidate.display,
                display_meta=candidate.description,
            )


def fuzzy_command_candidates(registry: SlashCommandRegistry, query: str):
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
    root = Path(root)
    count = 0
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix().lower()):
        if count >= limit:
            return
        if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if not rel:
            continue
        count += 1
        yield rel, path.is_dir()


def quote_mention_path(path: str) -> str:
    if any(ch.isspace() for ch in path):
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return path


def fuzzy_score(query: str, candidate: str) -> int:
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
