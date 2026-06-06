from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .base import AgentMiddleware


log = logging.getLogger("harness")


class MemoryMiddleware(AgentMiddleware):
    """Injects long-term memory context and runs Dream maintenance."""

    def __init__(self, workspace: str | Path, *, check_interval_seconds: float | None = None):
        self.workspace = Path(workspace).resolve()
        self._check_interval_seconds = check_interval_seconds
        self._next_dream_check_at = 0.0

    def on_conversation_start(self, messages: list[dict], runtime_state=None,
                              agent_name: str | None = None) -> list[dict]:
        if _memory_disabled():
            return []
        try:
            self.maybe_run_dream()
            store = self._store()
            if not store.exists() or not store.has_active_records():
                return []
            content = store.read_memory_file("MEMORY.md").strip()
            if not content:
                return []
            return [
                {
                    "role": "system",
                    "content": (
                        "Long-term memory navigation "
                        "(dynamic user-context, not stable prompt prefix):\n"
                        f"{content}"
                    ),
                }
            ]
        except Exception as exc:
            log.debug("Memory navigation skipped after error: %s", exc)
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

            from ...memory.recall import MemoryRecall

            recall = MemoryRecall(store)
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
