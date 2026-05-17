from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = []
        if not self.sessions_dir.exists():
            return sessions
        for metadata_path in self.sessions_dir.glob("*/session.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session_root = metadata_path.parent
            metadata.setdefault("id", session_root.name)
            metadata["events_path"] = str(session_root / "events.jsonl")
            sessions.append(metadata)
        sessions.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return sessions

    def read_metadata(self, session_id: str) -> dict[str, Any]:
        session_root = self._session_root(session_id)
        metadata_path = session_root / "session.json"
        if not metadata_path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        session_root = self._session_root(session_id)
        events_path = session_root / "events.jsonl"
        if not events_path.exists():
            raise FileNotFoundError(f"Session events not found: {session_id}")
        events = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events.append(json.loads(line))
        return events

    def _session_root(self, session_id: str) -> Path:
        if "/" in session_id or "\\" in session_id or session_id in {"", ".", ".."}:
            raise ValueError(f"Invalid session id: {session_id}")
        return self.sessions_dir / session_id

    @staticmethod
    def _new_session_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"
