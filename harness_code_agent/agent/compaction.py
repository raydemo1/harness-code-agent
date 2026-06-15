"""Compaction gate helpers."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .. import config

COALESCE_SECONDS = 30


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompactionThresholds:
    compact: int          # 85% — start lightweight auto-compaction


def get_thresholds(context_window: int | None = None) -> CompactionThresholds:
    w = context_window or config.CONTEXT_WINDOW_TOKENS
    return CompactionThresholds(
        compact=int(w * 0.85) if context_window is not None else config.COMPRESS_THRESHOLD,
    )


def compaction_action(token_count: int, thresholds: CompactionThresholds) -> str:
    """Determine what compaction action to take given current token usage."""
    if token_count >= thresholds.compact:
        return "auto_compact"
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
