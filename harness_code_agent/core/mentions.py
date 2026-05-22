from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..sessions.summary import load_session_summary
from ..sessions.store import SessionStore


FILE_CONTEXT_LIMIT = 60_000


@dataclass(frozen=True)
class Mention:
    raw: str
    kind: str
    target: str


@dataclass(frozen=True)
class ResolvedMention:
    raw: str
    kind: str
    target: str
    resolved: str
    content: str


class MentionResolutionError(ValueError):
    pass


def parse_mentions(text: str) -> list[Mention]:
    mentions: list[Mention] = []
    seen: set[tuple[str, str]] = set()
    for raw, token in _iter_mention_tokens(text):
        if not token:
            continue
        if token.startswith("session:"):
            target = token.removeprefix("session:")
            kind = "session"
        else:
            target = token
            kind = "file"
        key = (kind, target)
        if key in seen:
            continue
        seen.add(key)
        mentions.append(Mention(raw=raw, kind=kind, target=target))
    return mentions


def _iter_mention_tokens(text: str):
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "@" or (i > 0 and not text[i - 1].isspace()):
            i += 1
            continue
        start = i
        i += 1
        if i < n and text[i] == '"':
            i += 1
            chars = []
            escaped = False
            while i < n:
                ch = text[i]
                if escaped:
                    chars.append(ch)
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    i += 1
                    break
                else:
                    chars.append(ch)
                i += 1
            token = "".join(chars).strip()
            raw = text[start:i]
            if token:
                yield raw, token
            continue

        token_start = i
        while i < n and not text[i].isspace():
            i += 1
        token = text[token_start:i].rstrip(".,;:!?)]}")
        if not token or token.startswith("@"):
            continue
        raw = "@" + token
        yield raw, token



def resolve_mentions(
    text: str,
    *,
    workspace_root: str | Path,
    session_store: SessionStore,
) -> list[ResolvedMention]:
    root = Path(workspace_root).resolve()
    return [
        _resolve_file_mention(item, root)
        if item.kind == "file"
        else _resolve_session_mention(item, session_store)
        for item in parse_mentions(text)
    ]


def render_mention_context(resolved: list[ResolvedMention]) -> str:
    if not resolved:
        return ""
    parts = ["Mention context:"]
    for item in resolved:
        parts.append("")
        parts.append(f"- {item.raw} resolved as {item.kind}: {item.resolved}")
        parts.append("```text")
        parts.append(item.content)
        parts.append("```")
    return "\n".join(parts)


def format_turn_with_mentions(user_text: str, resolved: list[ResolvedMention]) -> str:
    context = render_mention_context(resolved)
    if not context:
        return user_text
    return f"{context}\n\nUser turn:\n{user_text}"


def _resolve_file_mention(mention: Mention, root: Path) -> ResolvedMention:
    if not mention.target:
        raise MentionResolutionError(f"Empty file mention: {mention.raw}")
    raw_path = Path(mention.target)
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    else:
        candidate = (root / raw_path).resolve()
    if not _is_relative_to(candidate, root):
        raise MentionResolutionError(f"File mention escapes workspace: {mention.raw}")
    if not candidate.exists() or not candidate.is_file():
        raise MentionResolutionError(f"File mention not found: {mention.raw}")
    content = candidate.read_text(encoding="utf-8", errors="replace")
    if len(content) > FILE_CONTEXT_LIMIT:
        total = len(content)
        content = content[:FILE_CONTEXT_LIMIT] + (
            f"\n\n[TRUNCATED] You are seeing {FILE_CONTEXT_LIMIT} of {total} total characters. "
            f"The remaining {total - FILE_CONTEXT_LIMIT} characters are not shown."
        )
    return ResolvedMention(
        raw=mention.raw,
        kind="file",
        target=mention.target,
        resolved=str(candidate),
        content=content,
    )


def _resolve_session_mention(
    mention: Mention,
    session_store: SessionStore,
) -> ResolvedMention:
    if not mention.target:
        raise MentionResolutionError(f"Empty session mention: {mention.raw}")
    try:
        content = load_session_summary(session_store, mention.target)
    except (FileNotFoundError, ValueError) as e:
        raise MentionResolutionError(f"Session mention not found: {mention.raw}") from e
    return ResolvedMention(
        raw=mention.raw,
        kind="session",
        target=mention.target,
        resolved=mention.target,
        content=content,
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
