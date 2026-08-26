"""Ordered, idempotent cleanup for resources owned by one session."""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleCloseError:
    name: str
    error: str


class LifecycleScope:
    """Own session resources and close them in explicit dependency order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[tuple[int, int, str, Callable[[], object]]] = []
        self._sequence = 0
        self._closed = False

    def register(self, name: str, close: Callable[[], object], *, order: int) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("lifecycle scope is closed")
            self._sequence += 1
            self._entries.append((int(order), self._sequence, str(name), close))

    def close(self) -> list[LifecycleCloseError]:
        with self._lock:
            if self._closed:
                return []
            self._closed = True
            entries = sorted(self._entries, key=lambda item: (item[0], item[1]), reverse=True)
            self._entries.clear()
        errors: list[LifecycleCloseError] = []
        for _order, _sequence, name, close in entries:
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - cleanup must continue through every owner
                errors.append(LifecycleCloseError(name, f"{type(exc).__name__}: {exc}"))
        return errors
