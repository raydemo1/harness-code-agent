from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from events import EventBus


@dataclass
class Session:
    id: str
    root: Path
    metadata_path: Path
    events_path: Path
    snapshots_dir: Path


class SessionStore:
    """Durable local session storage under a Harness state directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.sessions_dir = self.root / "sessions"

    def create(
        self,
        *,
        profile: str,
        cwd: str | Path,
        model: str,
        permission_mode: str,
    ) -> Session:
        session_id = self._new_session_id()
        session_root = self.sessions_dir / session_id
        snapshots_dir = session_root / "snapshots"
        session_root.mkdir(parents=True, exist_ok=False)
        snapshots_dir.mkdir(parents=True, exist_ok=True)

        session = Session(
            id=session_id,
            root=session_root,
            metadata_path=session_root / "session.json",
            events_path=session_root / "events.jsonl",
            snapshots_dir=snapshots_dir,
        )
        metadata = {
            "id": session.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "cwd": str(Path(cwd).resolve()),
            "profile": profile,
            "model": model,
            "permission_mode": permission_mode,
            "status": "running",
        }
        session.metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        session.events_path.write_text("", encoding="utf-8")
        return session

    def event_bus(self, session: Session) -> EventBus:
        return EventBus(session.events_path)

    @staticmethod
    def _new_session_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

