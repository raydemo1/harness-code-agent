"""Approval provider for the Textual TUI."""
from __future__ import annotations

import json
import re
import shlex
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..runtime.approvals import ApprovalRequest, ApprovalResult

if TYPE_CHECKING:
    from .app import TuiApp

_PYTHON_COMMANDS = {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}


class TuiApprovalProvider:
    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        app_tui: "TuiApp | None" = None,
        allowlist: "ApprovalAllowlist | None" = None,
    ):
        self.app_tui = app_tui
        self.allowlist = allowlist
        if self.allowlist is None and project_root is not None:
            self.allowlist = ApprovalAllowlist(project_root)

    def request(self, request: ApprovalRequest) -> ApprovalResult:
        persistent_prefix = _persistent_prefix_for_request(request)
        if self.allowlist is not None and request.tool_name == "run_bash":
            rule = self.allowlist.match(str(request.args.get("command", "")))
            if rule is not None:
                return ApprovalResult(
                    True,
                    "approved by project allowlist",
                    {
                        "ui": "tui",
                        "approval_source": "project_allowlist",
                        "prefix": rule.get("prefix", []),
                    },
                )

        if self.app_tui is None:
            return ApprovalResult(False, "no TUI app available", {"ui": "tui"})

        # Bridge: worker thread → UI thread via call_from_thread + Event
        event = threading.Event()
        result_holder: list = [None]

        def _show():
            self.app_tui.show_approval_panel(request, event, result_holder)

        try:
            self.app_tui.call_from_thread(_show)
        except Exception:
            return ApprovalResult(False, "failed to show approval panel", {"ui": "tui"})

        # Block until user makes a choice
        event.wait()

        approved = result_holder[0] if result_holder else False
        if approved:
            # If persist was used, the panel already handled it
            return ApprovalResult(True, "approved in TUI", {"ui": "tui"})
        return ApprovalResult(False, "denied in TUI", {"ui": "tui"})


class ApprovalAllowlist:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.path = self.project_root / ".harness" / "approval_allowlist.json"

    def add_prefix_rule(self, prefix: list[str], *, command: str) -> None:
        clean_prefix = [_normalize_token(token) for token in prefix if _normalize_token(token)]
        if not clean_prefix:
            return
        data = self._read()
        rules = data.setdefault("rules", [])
        for rule in rules:
            if rule.get("tool") == "run_bash" and rule.get("kind") == "prefix" and rule.get("prefix") == clean_prefix:
                return
        rules.append(
            {
                "tool": "run_bash",
                "kind": "prefix",
                "prefix": clean_prefix,
                "command": command,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._write(data)

    def match(self, command: str) -> dict | None:
        tokens = [_normalize_token(token) for token in _tokenize_command(command)]
        if not tokens:
            return None
        for rule in self._read().get("rules", []):
            if rule.get("tool") != "run_bash" or rule.get("kind") != "prefix":
                continue
            prefix = [str(token) for token in rule.get("prefix", [])]
            if prefix and tokens[: len(prefix)] == prefix:
                return rule
        return None

    def matches(self, command: str) -> bool:
        return self.match(command) is not None

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "rules": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "rules": []}
        if not isinstance(data, dict):
            return {"version": 1, "rules": []}
        if not isinstance(data.get("rules"), list):
            data["rules"] = []
        data["version"] = 1
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _persistent_prefix_for_request(request: ApprovalRequest) -> list[str] | None:
    if request.tool_name != "run_bash":
        return None
    return _derive_persistent_prefix(str(request.args.get("command", "")))


def _derive_persistent_prefix(command: str) -> list[str] | None:
    tokens = _tokenize_command(command)
    if len(tokens) < 2:
        return None
    normalized = [_normalize_token(token) for token in tokens]
    python_index = _first_python_token_index(normalized)
    if python_index is not None:
        if len(normalized) <= python_index + 2:
            return None
        if normalized[python_index + 1] == "-":
            return None
        return normalized[: python_index + 3]
    prefix_len = min(3, len(normalized))
    if prefix_len < 2:
        return None
    return normalized[:prefix_len]


def _tokenize_command(command: str) -> list[str]:
    if not command.strip() or re.search(r"[|;&<>]", command):
        return []
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return []


def _first_python_token_index(tokens: list[str]) -> int | None:
    for index, token in enumerate(tokens):
        if token in _PYTHON_COMMANDS:
            return index
    return None


def _normalize_token(token: object) -> str:
    return str(token).strip().strip("\"'").lower()
