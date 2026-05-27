"""Cancellation token for interrupting agent turns."""
from __future__ import annotations

import threading


class CancellationToken:
    """Thread-safe cancellation token passed through the agent loop."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise if cancelled. Call at loop boundaries."""
        if self._event.is_set():
            raise CancelledError("Turn cancelled by user")


class CancelledError(Exception):
    pass
