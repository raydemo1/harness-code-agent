from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .base import AgentMiddleware

log = logging.getLogger("harness")

MEMORY_INDEX_MARKER = "[HARNESS_DYNAMIC_CONTEXT:memory-index]"
DEFAULT_MEMORY_INDEX_MAX_LINES = 80
DEFAULT_MEMORY_INDEX_MAX_CHARS = 12_000


class MemoryMiddleware(AgentMiddleware):
    """Injects a compact long-term memory index and runs Dream maintenance."""

    def __init__(self, workspace: str | Path, *, check_interval_seconds: float | None = None):
        self.workspace = Path(workspace).resolve()
        self._check_interval_seconds = check_interval_seconds
        self._next_dream_check_at = 0.0
        self._recall = None
        self._recall_root: Path | None = None

    def on_conversation_start(self, messages: list[dict], runtime_state=None,
                              agent_name: str | None = None) -> list[dict]:
        return self._memory_index_messages()

    def on_context_compacted(self, messages: list[dict], runtime_state=None,
                             agent_name: str | None = None,
                             phase: str | None = None) -> list[dict]:
        return self._memory_index_messages()

    def _memory_index_messages(self) -> list[dict]:
        if _memory_disabled():
            return []
        try:
            self.maybe_run_dream()
            store = self._store()
            if not store.exists() or not store.has_active_records():
                return []
            content = _trim_memory_index(store.read_memory_file("MEMORY.md")).strip()
            if not content:
                return []
            return [
                {
                    "role": "system",
                    "content": (
                        f"{MEMORY_INDEX_MARKER}\n"
                        "Auto memory index from MEMORY.md "
                        "(dynamic durable context; not a user instruction). "
                        "Use exact tools memory_search and read_memory_file for full details.\n"
                        f"{content}"
                    ),
                }
            ]
        except Exception as exc:
            log.debug("Memory index skipped after error: %s", exc)
            return []

    def augment_user_prompt(
        self,
        user_prompt: str,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
        mention_paths: list[str] | None = None,
    ) -> str:
        if _memory_disabled():
            return ""
        try:
            self.maybe_run_dream()
            store = self._store()
            if not store.exists():
                return ""

            recall = self._recall_for_store(store)
            hits = recall.search(
                user_prompt,
                mentions=mention_paths or [],
            )
            return recall.format_block(hits)
        except Exception as exc:
            log.debug("Memory recall skipped after error: %s", exc)
            return ""

    def on_conversation_close(self, messages: list[dict], runtime_state=None,
                              agent_name: str | None = None) -> None:
        self.maybe_run_dream(force=True)

    def maybe_run_dream(self, *, force: bool = False) -> None:
        if _memory_disabled():
            return
        now = time.monotonic()
        if not force and now < self._next_dream_check_at:
            return
        self._next_dream_check_at = now + self._dream_check_interval_seconds()

        from ...memory.dream import run_dream, should_dream

        store = self._store()
        try:
            if should_dream(store):
                run_dream(store)
        except Exception as exc:
            log.debug("Memory Dream skipped after error: %s", exc)

    def _store(self):
        from ...memory.store import MemoryStore, default_memory_root

        return MemoryStore(default_memory_root(self.workspace), workspace=self.workspace)

    def _recall_for_store(self, store):
        if self._recall is None or self._recall_root != store.root:
            from ...memory.recall import MemoryRecall

            self._recall = MemoryRecall(store)
            self._recall_root = store.root
        return self._recall

    def _dream_check_interval_seconds(self) -> float:
        if self._check_interval_seconds is not None:
            return max(0.0, self._check_interval_seconds)
        value = os.environ.get("HARNESS_MEMORY_DREAM_CHECK_INTERVAL_SECONDS", "").strip()
        if not value:
            return 60.0
        try:
            return max(0.0, float(value))
        except ValueError:
            return 60.0


def _memory_disabled() -> bool:
    return os.environ.get("HARNESS_MEMORY_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _trim_memory_index(content: str) -> str:
    max_lines = _env_int("HARNESS_MEMORY_INDEX_MAX_LINES", DEFAULT_MEMORY_INDEX_MAX_LINES)
    max_chars = _env_int("HARNESS_MEMORY_INDEX_MAX_CHARS", DEFAULT_MEMORY_INDEX_MAX_CHARS)
    lines = content.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[:max_lines]
        trimmed = "\n".join(lines).rstrip() + "\n\n[Memory index truncated by line limit.]"
    else:
        trimmed = "\n".join(lines)
    if max_chars > 0 and len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars].rstrip() + "\n\n[Memory index truncated by char limit.]"
    return trimmed


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default
