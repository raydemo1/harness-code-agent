from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorkspaceWriteResult:
    path: Path
    snapshot_path: Path | None


class WorkspaceService:
    """Workspace path resolution and file snapshot service."""

    DEFAULT_PROTECTED_NAMES = {".env", ".env.local", ".env.production"}

    def __init__(
        self,
        *,
        root: str | Path,
        snapshots_dir: str | Path | None = None,
        protected_names: set[str] | None = None,
    ):
        self.root = Path(root).resolve()
        self.snapshots_dir = Path(snapshots_dir).resolve() if snapshots_dir else self.root / ".harness" / "snapshots"
        self.protected_names = protected_names or self.DEFAULT_PROTECTED_NAMES
        self.changed_files: list[Path] = []

    def resolve(self, path: str | Path) -> Path:
        raw = Path(path)
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            candidate = (self.root / raw).resolve()
        if not self._is_relative_to(candidate, self.root):
            raise ValueError(f"Path escapes workspace: {path}")
        return candidate

    def read_text(self, path: str | Path) -> str:
        return self.resolve(path).read_text(encoding="utf-8", errors="replace")

    def write_text(self, path: str | Path, content: str) -> WorkspaceWriteResult:
        resolved = self.resolve(path)
        self._ensure_writable(resolved)
        snapshot_path = self.snapshot(path) if resolved.exists() else None
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        rel = resolved.relative_to(self.root)
        if rel not in self.changed_files:
            self.changed_files.append(rel)
        return WorkspaceWriteResult(path=resolved, snapshot_path=snapshot_path)

    def snapshot(self, path: str | Path) -> Path | None:
        resolved = self.resolve(path)
        if not resolved.exists() or not resolved.is_file():
            return None
        rel = resolved.relative_to(self.root)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        snapshot_path = self.snapshots_dir / rel.parent / f"{resolved.name}.{stamp}.bak"
        suffix = 1
        while snapshot_path.exists():
            snapshot_path = self.snapshots_dir / rel.parent / f"{resolved.name}.{stamp}.{suffix}.bak"
            suffix += 1
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, snapshot_path)
        return snapshot_path

    _SAFE_ENV_SUFFIXES = {'.example', '.template', '.sample', '.default', '.dist'}

    def _ensure_writable(self, path: Path) -> None:
        rel = path.relative_to(self.root)
        parts = rel.parts
        if ".git" in parts:
            raise ValueError(f"Refusing to write inside .git: {rel}")
        if self._is_protected_name(path.name):
            raise ValueError(f"Refusing to write protected file: {rel}")

    def _is_protected_name(self, name: str) -> bool:
        if name in self.protected_names:
            return True
        for p in self.protected_names:
            if name.startswith(p + '.') and not any(name.endswith(s) for s in self._SAFE_ENV_SUFFIXES):
                return True
        return False

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

