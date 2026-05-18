from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SessionEvent:
    sequence: int
    timestamp: float
    type: str
    agent: str | None
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": self.type,
            "agent": self.agent,
            "payload": self.payload,
        }


class EventBus:
    """Append-only event stream for product runtime observability."""

    def __init__(self, events_path: str | Path | None = None):
        self.events_path = Path(events_path) if events_path is not None else None
        self.events: list[SessionEvent] = []
        self._sequence = 0
        if self.events_path is not None:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        event_type: str,
        *,
        agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SessionEvent:
        self._sequence += 1
        event = SessionEvent(
            sequence=self._sequence,
            timestamp=time.time(),
            type=event_type,
            agent=agent,
            payload=payload or {},
        )
        self.events.append(event)
        if self.events_path is not None:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return event

