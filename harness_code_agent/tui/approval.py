from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from pprint import pformat
from textwrap import fill
from typing import Callable

from prompt_toolkit import print_formatted_text
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from ..runtime.approvals import ApprovalRequest, ApprovalResult


_CHOICES = (
    ("approve", "Approve"),
    ("persist", "Persist project rule"),
    ("deny", "Deny"),
)
_DEFAULT_CHOICE_INDEX = 0
_PYTHON_COMMANDS = {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}

_APPROVAL_STYLE = Style.from_dict(
    {
        "approval.choice": "ansigray",
        "approval.choice.selected": "reverse ansimagenta bold",
        "approval.help": "ansigray",
    }
)


class TuiApprovalProvider:
    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        choice_bar_factory: Callable[..., "ApprovalChoiceBar"] | None = None,
        allowlist: "ApprovalAllowlist" | None = None,
    ):
        self.choice_bar_factory = choice_bar_factory or ApprovalChoiceBar
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

        show_details = False
        while True:
            try:
                choice = self.choice_bar_factory(
                    request,
                    show_details=show_details,
                    persistent_prefix=persistent_prefix,
                ).run()
            except (EOFError, KeyboardInterrupt):
                print_formatted_text(HTML("\n<ansired>Approval cancelled/interrupted.</ansired>"))
                return ApprovalResult(False, "interrupted in TUI", {"ui": "tui"})

            if choice == "approve":
                return ApprovalResult(True, "approved in TUI", {"ui": "tui"})
            if choice == "persist":
                if self.allowlist is not None and persistent_prefix:
                    self.allowlist.add_prefix_rule(
                        persistent_prefix,
                        command=str(request.args.get("command", "")),
                    )
                    return ApprovalResult(
                        True,
                        "approved and persisted project command prefix",
                        {
                            "ui": "tui",
                            "approval_source": "project_allowlist",
                            "persisted": True,
                            "prefix": persistent_prefix,
                        },
                    )
                return ApprovalResult(
                    True,
                    "approved in TUI; no persistent prefix available",
                    {"ui": "tui", "persisted": False},
                )
            if choice in {None, "deny"}:
                return ApprovalResult(False, "denied in TUI", {"ui": "tui"})
            if choice == "details":
                show_details = True
                continue


class ApprovalChoiceBar:
    def __init__(
        self,
        request: ApprovalRequest,
        *,
        show_details: bool = False,
        persistent_prefix: list[str] | None = None,
    ):
        self.request = request
        self.show_details = show_details
        self.persistent_prefix = persistent_prefix
        self.selected_index = _DEFAULT_CHOICE_INDEX

    def run(self) -> str:
        bindings = KeyBindings()

        @bindings.add("right")
        @bindings.add("down")
        @bindings.add("tab")
        def _(event):
            self.selected_index = (self.selected_index + 1) % len(_CHOICES)

        @bindings.add("left")
        @bindings.add("up")
        @bindings.add("s-tab")
        def _(event):
            self.selected_index = (self.selected_index - 1) % len(_CHOICES)

        @bindings.add("enter")
        def _(event):
            event.app.exit(result=_CHOICES[self.selected_index][0])

        @bindings.add("y")
        def _(event):
            event.app.exit(result="approve")

        @bindings.add("p")
        def _(event):
            event.app.exit(result="persist")

        @bindings.add("n")
        def _(event):
            event.app.exit(result="deny")

        @bindings.add("d")
        def _(event):
            event.app.exit(result="details")

        @bindings.add("c-c")
        def _(event):
            event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("c-d")
        def _(event):
            event.app.exit(exception=EOFError())

        body = Window(
            FormattedTextControl(
                lambda: _format_approval_body(
                    self.request,
                    show_details=self.show_details,
                    persistent_prefix=self.persistent_prefix,
                )
            ),
            wrap_lines=True,
        )
        spacer = Window(height=1)
        choices = Window(
            FormattedTextControl(
                lambda: _format_choice_bar(
                    self.selected_index,
                    persistent_prefix=self.persistent_prefix,
                )
            ),
            height=1,
        )
        root = HSplit([body, spacer, choices])
        app: Application[str] = Application(
            layout=Layout(root),
            key_bindings=bindings,
            full_screen=False,
            erase_when_done=False,
            style=_APPROVAL_STYLE,
        )
        return app.run() or "deny"


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


def _format_approval_body(
    request: ApprovalRequest,
    *,
    show_details: bool,
    persistent_prefix: list[str] | None = None,
) -> str:
    lines = [
        "",
        "Approval required",
        f"tool: {request.tool_name}",
        f"risk: {request.risk}",
        *_wrap_field("reason", request.reason),
    ]
    if request.tool_name == "run_bash":
        lines.extend(_wrap_field("command", request.args.get("command", "")))
        if persistent_prefix:
            lines.extend(_wrap_field("project rule", _prefix_display(persistent_prefix)))
        else:
            lines.append("project rule: unavailable for this command")
    else:
        lines.extend(_format_args_summary(request.args))

    if show_details:
        lines.append("")
        lines.append("details:")
        lines.extend(f"  {line}" for line in pformat(request.args, width=88).splitlines())
    return "\n".join(lines)


def _format_choice_bar(selected_index: int, *, persistent_prefix: list[str] | None = None) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = [("class:approval.help", "Use arrows/tab, Enter to choose  ")]
    for index, (value, label) in enumerate(_CHOICES):
        if value == "persist" and not persistent_prefix:
            label = "Persist unavailable"
        style = "class:approval.choice.selected" if index == selected_index else "class:approval.choice"
        fragments.append((style, f" {label} "))
        fragments.append(("", " "))
    fragments.append(("class:approval.help", " shortcuts: y approve · p persist · n deny · d details"))
    return fragments


def _format_args_summary(args: dict) -> list[str]:
    summary = _summarize_args(args)
    lines = ["args:"]
    for key, value in summary.items():
        lines.extend(_wrap_field(f"  {key}", value))
    return lines


def _wrap_field(label: str, value: object, *, width: int = 100) -> list[str]:
    prefix = f"{label}: "
    text = str(value)
    if not text:
        return [prefix.rstrip()]
    wrapped = fill(
        prefix + text,
        width=width,
        subsequent_indent=" " * len(prefix),
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapped.splitlines()


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


def _prefix_display(prefix: list[str]) -> str:
    return " ".join(prefix)


def _summarize_args(args: dict) -> dict:
    summary = dict(args or {})
    if "content" in summary:
        summary["content"] = f"[{len(str(summary['content']))} chars]"
    return summary
