from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


MEMORY_CONTENT_FILES = (
    "project.md",
    "decisions.md",
    "commands.md",
    "debugging.md",
    "preferences.md",
    "learnings.md",
)
MEMORY_FILES = (
    "MEMORY.md",
    *MEMORY_CONTENT_FILES,
    "inbox.jsonl",
    "records.jsonl",
    "manifest.json",
    "dream-log.md",
)
PROTECTED_FILES = {"MEMORY.md", "manifest.json", "dream-log.md", "records.jsonl", "inbox.jsonl"}
LOCK_STALE_SECONDS = _env_int("HARNESS_MEMORY_LOCK_STALE_SECONDS", 300)

_PROCESS_LOCK = threading.RLock()


@dataclass
class MemoryRecord:
    id: str
    file: str
    anchor: str
    title: str
    summary: str
    tags: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    source_sessions: list[str] = field(default_factory=list)
    confidence: float = 0.5
    status: str = "active"
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryRecord":
        known = {field.name for field in cls.__dataclass_fields__.values()}
        payload = {key: value for key, value in data.items() if key in known}
        return cls(**payload)

    def to_dict(self) -> dict:
        return asdict(self)


def default_memory_root(workspace: str | Path) -> Path:
    override = os.environ.get("HARNESS_MEMORY_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    key = resolve_repo_key(Path(workspace))
    return Path.home() / ".harness" / "projects" / key / "memory"


def resolve_repo_key(workspace: Path) -> str:
    workspace = workspace.resolve()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        common_dir = Path(proc.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = (workspace / common_dir).resolve()
        basis = str(common_dir)
    except Exception:
        basis = str(workspace)
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in workspace.name) or "workspace"
    return f"{name}-{digest}"


class MemoryStore:
    def __init__(self, root: str | Path, *, workspace: str | Path | None = None):
        self.root = Path(root).expanduser().resolve()
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self._lock_path = self.root / ".memory.lock"

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with _PROCESS_LOCK:
            acquired = False
            while not acquired:
                try:
                    fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, f"{os.getpid()} {time.time()}\n".encode("utf-8"))
                    os.close(fd)
                    acquired = True
                except FileExistsError:
                    try:
                        age = time.time() - self._lock_path.stat().st_mtime
                        if age > LOCK_STALE_SECONDS:
                            self._lock_path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        continue
                    time.sleep(0.05)
            try:
                yield
            finally:
                self._lock_path.unlink(missing_ok=True)

    def ensure_initialized(self) -> None:
        with self.lock():
            for filename in MEMORY_FILES:
                path = self._path(filename)
                if path.exists():
                    continue
                if filename == "manifest.json":
                    manifest = {
                        "version": 1,
                        "repo_key": resolve_repo_key(self.workspace) if self.workspace else "",
                        "repo_path": str(self.workspace) if self.workspace else "",
                        "created_at": _utc_now(),
                        "updated_at": _utc_now(),
                        "last_dream_at": "",
                        "active_record_count": 0,
                        "inbox_count": 0,
                    }
                    self.atomic_write(filename, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
                elif filename == "MEMORY.md":
                    self.atomic_write(filename, _initial_memory_md())
                else:
                    self.atomic_write(filename, "")

    def exists(self) -> bool:
        return self.root.exists()

    def has_active_records(self) -> bool:
        return any(record.status == "active" for record in self.read_records()) if self.exists() else False

    def read_manifest(self) -> dict:
        path = self._path("manifest.json")
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8") or "{}")

    def read_memory_file(self, filename: str) -> str:
        path = self._path(filename)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def read_records(self) -> list[MemoryRecord]:
        return [
            MemoryRecord.from_dict(obj)
            for obj in _read_jsonl(self._path("records.jsonl"))
        ]

    def read_inbox(self) -> list[dict]:
        return _read_jsonl(self._path("inbox.jsonl"))

    def append_candidate(self, candidate: dict) -> None:
        self.ensure_initialized()
        with self.lock():
            with self._path("inbox.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(candidate, ensure_ascii=False) + "\n")
            manifest = self.read_manifest()
            manifest["inbox_count"] = len(self.read_inbox())
            manifest["updated_at"] = _utc_now()
            self.atomic_write("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    def atomic_write_records(self, records: list[MemoryRecord]) -> None:
        text = "".join(json.dumps(record.to_dict(), ensure_ascii=False) + "\n" for record in records)
        self.atomic_write("records.jsonl", text)

    def clear_inbox(self) -> None:
        self.atomic_write("inbox.jsonl", "")

    def atomic_write(self, filename: str, content: str) -> None:
        target = self._path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            os.replace(tmp_name, target)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _path(self, filename: str) -> Path:
        if filename not in MEMORY_FILES and filename != ".memory.lock":
            raise ValueError(f"Unknown memory file: {filename}")
        path = (self.root / filename).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Memory path escapes root: {filename}") from exc
        return path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _initial_memory_md() -> str:
    return "\n".join(
        [
            "# Long-Term Memory",
            "",
            "This file is a navigation surface for generated memory records.",
            "",
            "## Memory Files",
            "",
            "| File | Purpose |",
            "|------|---------|",
            "| project.md | Architecture, modules, conventions, project facts |",
            "| decisions.md | Design decisions, tradeoffs, rationale |",
            "| commands.md | Useful commands and workflows |",
            "| debugging.md | Debug observations, failures, fixes |",
            "| preferences.md | User preferences and work style |",
            "| learnings.md | Agent experiences and mental summaries |",
            "",
        ]
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
