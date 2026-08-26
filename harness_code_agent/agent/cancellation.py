"""Cancellation token for interrupting agent turns."""
from __future__ import annotations

import threading
from collections.abc import Callable


class CancellationToken:
    """Thread-safe cancellation token passed through the agent loop."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            callback()

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self._event.is_set():
                run_now = True
            else:
                self._callbacks.append(callback)
                run_now = False
        if run_now:
            callback()

        def remove() -> None:
            with self._lock:
                try:
                    self._callbacks.remove(callback)
                except ValueError:
                    pass

        return remove

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise if cancelled. Call at loop boundaries."""
        if self._event.is_set():
            raise CancelledError("Turn cancelled by user")


class CancelledError(Exception):
    pass
