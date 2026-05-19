from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..sessions.store import SessionStore


MENTION_RE = re.compile(r"(?<!\S)@([^\s]+)")
FILE_CONTEXT_LIMIT = 60_000
SESSION_EVENT_LIMIT = 8


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
    for match in MENTION_RE.finditer(text):
        token = match.group(1).rstrip(".,;:!?)]}")
        if not token:
            continue
        raw = f"@{token}"
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
        metadata = session_store.read_metadata(mention.target)
        events = session_store.read_events(mention.target)
    except (FileNotFoundError, ValueError) as e:
        raise MentionResolutionError(f"Session mention not found: {mention.raw}") from e

    lines = [
        f"id: {metadata.get('id', mention.target)}",
        f"profile: {metadata.get('profile', '')}",
        f"model: {metadata.get('model', '')}",
        f"permission_mode: {metadata.get('permission_mode', '')}",
        f"status: {metadata.get('status', '')}",
        f"cwd: {metadata.get('cwd', '')}",
        f"created_at: {metadata.get('created_at', '')}",
        f"events: {len(events)}",
    ]
    if metadata.get("forked_from"):
        lines.append(f"forked_from: {metadata.get('forked_from')}")
    if metadata.get("resumed_from"):
        lines.append(f"resumed_from: {metadata.get('resumed_from')}")
    lines.append("recent_events:")
    for event in events[-SESSION_EVENT_LIMIT:]:
        lines.append(f"- {_event_summary(event)}")
    if not events:
        lines.append("- none")
    return ResolvedMention(
        raw=mention.raw,
        kind="session",
        target=mention.target,
        resolved=mention.target,
        content="\n".join(lines),
    )


def _event_summary(event: dict) -> str:
    payload = event.get("payload") or {}
    payload_bits = []
    for key in sorted(payload)[:4]:
        value = payload[key]
        text = str(value).replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        payload_bits.append(f"{key}={text}")
    suffix = f" ({', '.join(payload_bits)})" if payload_bits else ""
    return f"#{event.get('sequence')} {event.get('type')} agent={event.get('agent')}{suffix}"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
