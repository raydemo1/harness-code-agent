"""Compaction gate and manager — controls when and how context compaction happens.

CompactionGate: tracks active tool calls, dirty state, message revision, coalescing.
CompactionManager: generates async candidates, validates revision on commit, persists results.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import config

log = logging.getLogger("harness")

COALESCE_SECONDS = 30


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompactionThresholds:
    observe: int    # 60% — log only
    prepare: int    # 68% — async candidate generation
    allow: int      # 75% — allow boundary commit / sync fallback
    force: int      # 82% — forced synchronous compaction


def get_thresholds(context_window: int | None = None) -> CompactionThresholds:
    w = context_window or config.CONTEXT_WINDOW_TOKENS
    return CompactionThresholds(
        observe=int(w * 0.60),
        prepare=int(w * 0.68),
        allow=int(w * 0.75),
        force=int(w * 0.82),
    )


def compaction_action(token_count: int, thresholds: CompactionThresholds) -> str:
    """Determine what compaction action to take given current token usage."""
    if token_count >= thresholds.force:
        return "force_sync"
    if token_count >= thresholds.allow:
        return "sync_compact"
    if token_count >= thresholds.prepare:
        return "async_prepare"
    return "none"


# ---------------------------------------------------------------------------
# CompactionGate
# ---------------------------------------------------------------------------

class CompactionGate:
    """Tracks whether compaction is safe to perform right now."""

    def __init__(self) -> None:
        self._active_tool_calls: int = 0
        self.revision: int = 0
        self.dirty: bool = False
        self._last_compact_time: float = 0.0

    def can_compact(self, *, coalesce_seconds: int = COALESCE_SECONDS) -> bool:
        if self._active_tool_calls > 0:
            return False
        if coalesce_seconds > 0:
            elapsed = time.time() - self._last_compact_time
            if elapsed < coalesce_seconds:
                return False
        return True

    def begin_tool_call(self) -> None:
        self._active_tool_calls += 1
        self.dirty = True

    def end_tool_call(self) -> None:
        self._active_tool_calls = max(0, self._active_tool_calls - 1)

    def bump_revision(self) -> None:
        self.revision += 1
        self.dirty = True

    def mark_dirty(self) -> None:
        self.dirty = True

    def mark_compacted(self) -> None:
        self.dirty = False
        self._last_compact_time = time.time()


# ---------------------------------------------------------------------------
# CompactionManager — async candidate + revision-guarded commit
# ---------------------------------------------------------------------------

@dataclass
class CompactionCandidate:
    summary: str
    revision: int
    split_index: int
    old_count: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class CommitResult:
    committed: bool
    summary: str = ""
    reason: str = ""
    messages: list[dict] | None = None


class CompactionManager:
    """Manages compaction candidates and persists compaction history."""

    def __init__(self, compacted_dir: Path) -> None:
        self.compacted_dir = compacted_dir
        self.compacted_dir.mkdir(parents=True, exist_ok=True)
        (self.compacted_dir / "history").mkdir(parents=True, exist_ok=True)
        self._candidate: CompactionCandidate | None = None
        self._future: Future | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harness-compact")

    @property
    def candidate(self) -> CompactionCandidate | None:
        self.poll_candidate()
        return self._candidate

    @property
    def has_pending_async(self) -> bool:
        return self._future is not None and not self._future.done()

    def generate_candidate(
        self,
        messages: list[dict],
        *,
        llm_call: Callable,
        split_index: int,
        revision: int,
    ) -> CompactionCandidate:
        """Generate a compaction candidate by summarizing messages[:split_index]."""
        old = messages[:split_index]
        if old and old[0].get("role") == "system":
            old = old[1:]
        old_text = _messages_to_text(old)
        summary = llm_call([
            {"role": "system", "content": (
                "You are a concise summarizer. Preserve: key decisions, "
                "files created/modified, current progress, and errors encountered."
            )},
            {"role": "user", "content": old_text},
        ])
        candidate = CompactionCandidate(
            summary=summary,
            revision=revision,
            split_index=split_index,
            old_count=len(old),
        )
        self._candidate = candidate
        return candidate

    def prepare_candidate_async(
        self,
        messages: list[dict],
        *,
        llm_call: Callable,
        split_index: int,
        revision: int,
    ) -> bool:
        """Start background candidate generation if one is not already running."""
        self.poll_candidate()
        if self.has_pending_async:
            return False
        snapshot = [dict(msg) for msg in messages]
        self._future = self._executor.submit(
            self.generate_candidate,
            snapshot,
            llm_call=llm_call,
            split_index=split_index,
            revision=revision,
        )
        return True

    def poll_candidate(self) -> CompactionCandidate | None:
        if self._future is None or not self._future.done():
            return self._candidate
        future = self._future
        self._future = None
        try:
            self._candidate = future.result()
        except Exception as exc:
            log.warning("Async compaction candidate failed: %s", exc)
        return self._candidate

    def commit_candidate(
        self,
        candidate: CompactionCandidate,
        *,
        current_revision: int,
    ) -> CommitResult:
        """Commit candidate if revision matches; discard if stale."""
        if candidate.revision != current_revision:
            log.info(
                "Compaction candidate stale (candidate rev=%d, current rev=%d), discarding.",
                candidate.revision,
                current_revision,
            )
            return CommitResult(
                committed=False,
                reason=f"Stale candidate: revision {candidate.revision} != current {current_revision}",
            )

        self._persist_candidate(candidate)
        self._candidate = None
        log.info("Compaction committed (rev=%d, summary=%d chars).", candidate.revision, len(candidate.summary))
        return CommitResult(committed=True, summary=candidate.summary)

    def commit_candidate_to_messages(
        self,
        candidate: CompactionCandidate,
        messages: list[dict],
        *,
        current_revision: int,
    ) -> CommitResult:
        """Persist a candidate and replace the summarized prefix in messages."""
        result = self.commit_candidate(candidate, current_revision=current_revision)
        if not result.committed:
            return result

        system = [messages[0]] if messages and messages[0].get("role") == "system" else []
        split_index = max(len(system), min(candidate.split_index, len(messages)))
        recent = messages[split_index:]
        summary_msg = {
            "role": "user",
            "content": f"[COMPACTED CONTEXT — summary of earlier work]\n{candidate.summary}",
        }
        result.messages = system + [summary_msg] + recent
        return result

    def _persist_candidate(self, candidate: CompactionCandidate) -> None:
        # Write latest.md
        latest_path = self.compacted_dir / "latest.md"
        latest_path.write_text(candidate.summary, encoding="utf-8")

        # Write history entry
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(candidate.timestamp))
        history_path = self.compacted_dir / "history" / f"{stamp}-rev{candidate.revision}.md"
        history_path.write_text(candidate.summary, encoding="utf-8")

        # Append to index.jsonl
        index_path = self.compacted_dir / "index.jsonl"
        entry = {
            "timestamp": candidate.timestamp,
            "revision": candidate.revision,
            "split_index": candidate.split_index,
            "old_count": candidate.old_count,
            "summary_chars": len(candidate.summary),
            "file": history_path.name,
        }
        with index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_latest_summary(self) -> str | None:
        latest_path = self.compacted_dir / "latest.md"
        if latest_path.exists():
            return latest_path.read_text(encoding="utf-8")
        return None

    def get_history(self) -> list[dict]:
        index_path = self.compacted_dir / "index.jsonl"
        if not index_path.exists():
            return []
        entries = []
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def _messages_to_text(messages: list[dict]) -> str:
    """Flatten messages into readable text for summarization."""
    parts = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        if content:
            parts.append(f"[{role}] {content[:3000]}")
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            parts.append(f"[tool_call] {fn.get('name', '?')}({fn.get('arguments', '')[:500]})")
    return "\n".join(parts)
