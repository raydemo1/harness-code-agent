"""Session-visible tracking for tool work that can outlive a cancelled turn."""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, wait


class ToolTaskSupervisor:
    """Track running tool futures behind a small cancellation/cleanup interface."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._futures: set[Future] = set()
        self._closed = False

    def track(self, future: Future) -> None:
        with self._lock:
            if self._closed:
                future.cancel()
                raise RuntimeError("tool task supervisor is closed")
            self._futures.add(future)
        future.add_done_callback(self._forget)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(not future.done() for future in self._futures)

    def close(self, *, timeout: float = 1.0) -> bool:
        with self._lock:
            self._closed = True
            futures = tuple(self._futures)
        for future in futures:
            future.cancel()
        if futures:
            wait(futures, timeout=max(0.0, timeout))
        return all(future.done() for future in futures)

    def wait_idle(self, *, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                futures = tuple(future for future in self._futures if not future.done())
            if not futures:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait(futures, timeout=remaining)

    def _forget(self, future: Future) -> None:
        with self._lock:
            self._futures.discard(future)
