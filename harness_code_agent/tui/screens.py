"""Inline approval and question panels for the TUI."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, Static

if TYPE_CHECKING:
    from ..runtime.approvals import ApprovalRequest
    from ..runtime.questions import QuestionRequest


# ── ApprovalPanel ───────────────────────────────────────────────────────────

_APPROVE = 0
_PERSIST = 1
_DENY = 2

_APPROVAL_LABELS = ["Approve", "Persist", "Deny"]


class ApprovalPanel(Vertical):
    """Inline approval panel that replaces the input area.

    Double-key-press behavior: first press selects, second press submits.
    """

    can_focus = True

    DEFAULT_CSS = """
    ApprovalPanel {
        height: auto;
        border: solid #b16286;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self, request: ApprovalRequest, **kwargs):
        super().__init__(**kwargs)
        self._request = request
        self._selected_index = _APPROVE
        self._armed_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._format_body(), classes="body")
        yield Static(self._format_choices(), id="approval-choices")

    def _format_body(self) -> str:
        req = self._request
        lines = [
            f"[bold]⚠ Approval required[/]",
            f"tool: {req.tool_name}",
            f"risk: {req.risk}",
        ]
        if req.reason:
            lines.append(f"reason: {req.reason}")
        if req.tool_name == "run_bash":
            cmd = req.args.get("command", "")
            lines.append(f"$ {cmd}")
        else:
            for key, value in (req.args or {}).items():
                val_str = str(value)
                if len(val_str) > 200:
                    val_str = val_str[:197] + "..."
                lines.append(f"  {key}: {val_str}")
        return "\n".join(lines)

    def _format_choices(self) -> str:
        parts = []
        for i, label in enumerate(_APPROVAL_LABELS):
            marker = "▶" if i == self._selected_index else " "
            parts.append(f"{marker} [{i + 1}] {label}")
        return "   ".join(parts)

    def _refresh_choices(self) -> None:
        try:
            self.query_one("#approval-choices", Static).update(self._format_choices())
        except Exception:
            pass

    def on_key(self, event) -> None:
        if self.handle_key(event.key):
            event.prevent_default()
            event.stop()

    def handle_key(self, key: str) -> bool:
        """Handle a key press routed from either this panel or the app."""

        # Number keys 1-3: double-press to submit
        if key.isdigit() and 1 <= int(key) <= 3:
            num = int(key)
            target = num - 1
            if self._selected_index == target and self._armed_key == key:
                self._submit()
                return True
            self._selected_index = target
            self._armed_key = key
            self._refresh_choices()
            return True

        # Arrow keys
        if key == "left":
            self._selected_index = (self._selected_index - 1) % 3
            self._armed_key = None
            self._refresh_choices()
            return True
        elif key == "right":
            self._selected_index = (self._selected_index + 1) % 3
            self._armed_key = None
            self._refresh_choices()
            return True
        elif key == "enter":
            self._submit()
            return True
        elif key == "escape":
            self._deny()
            return True
        return False

    def _submit(self) -> None:
        if self._selected_index == _APPROVE:
            self.app._on_approval_result(True)
        elif self._selected_index == _PERSIST:
            from .approval import TuiApprovalProvider, _persistent_prefix_for_request
            provider = TuiApprovalProvider(project_root=self.app.cwd)
            persistent_prefix = _persistent_prefix_for_request(self._request)
            if provider.allowlist is not None and persistent_prefix:
                provider.allowlist.add_prefix_rule(
                    persistent_prefix,
                    command=str(self._request.args.get("command", "")),
                )
            self.app._on_approval_result(True)
        else:
            self.app._on_approval_result(False)

    def _deny(self) -> None:
        self.app._on_approval_result(False)


# ── QuestionPanel ───────────────────────────────────────────────────────────

class QuestionPanel(Vertical):
    """Inline question panel that replaces the input area.

    Double-key-press behavior: first press selects, second press submits.
    """

    can_focus = True

    DEFAULT_CSS = """
    QuestionPanel {
        height: auto;
        border: solid #3874cb;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self, request: QuestionRequest, **kwargs):
        super().__init__(**kwargs)
        self._request = request
        self._selected_index = 0
        self._other_text = ""
        self._armed_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._format_body(), id="q-body", classes="body")
        yield Static(self._format_choices(), id="q-choices")
        yield Input(placeholder="Other...", id="q-other-input")

    def on_mount(self) -> None:
        other = self.query_one("#q-other-input", Input)
        other.display = False

    def _format_body(self) -> str:
        req = self._request
        lines = [f"[bold]? {req.question}[/]"]
        for i, opt in enumerate(req.options, 1):
            desc = f" — {opt.description}" if opt.description else ""
            lines.append(f"  [{i}] {opt.label}{desc}")
        return "\n".join(lines)

    def _format_choices(self) -> str:
        parts = []
        for i, opt in enumerate(self._request.options, 1):
            marker = "▶" if i - 1 == self._selected_index else " "
            parts.append(f"{marker} [{i}] {opt.label}")
        return "   ".join(parts)

    def _refresh_choices(self) -> None:
        try:
            self.query_one("#q-choices", Static).update(self._format_choices())
        except Exception:
            pass

    def _update_other_visibility(self) -> None:
        opt = self._request.options[self._selected_index]
        other = self.query_one("#q-other-input", Input)
        if opt.is_other:
            other.display = True
        else:
            other.display = False

    def on_key(self, event) -> None:
        if self.handle_key(event.key):
            event.prevent_default()
            event.stop()

    def handle_key(self, key: str) -> bool:
        """Handle a key press routed from either this panel or the app."""

        # Number keys 1-9: double-press to submit
        if key.isdigit() and 1 <= int(key) <= len(self._request.options):
            num = int(key)
            target = num - 1
            # If on "Other" and text is being typed, treat as text input
            if self._request.options[self._selected_index].is_other and (
                self._other_text or self._armed_key is None or self._armed_key != key
            ):
                self._other_text += key
                self._armed_key = None
                return True
            # Double-press submits
            if self._selected_index == target and self._armed_key == key:
                self._submit()
                return True
            self._selected_index = target
            self._armed_key = key
            self._refresh_choices()
            self._update_other_visibility()
            return True

        if key == "up":
            self._selected_index = (self._selected_index - 1) % len(self._request.options)
            self._armed_key = None
            self._refresh_choices()
            self._update_other_visibility()
            return True
        elif key == "down":
            self._selected_index = (self._selected_index + 1) % len(self._request.options)
            self._armed_key = None
            self._refresh_choices()
            self._update_other_visibility()
            return True
        elif key == "enter":
            self._submit()
            return True
        elif key == "escape":
            self._cancel()
            return True
        return False

    def _submit(self) -> None:
        from ..runtime.questions import question_result_from_option
        opt = self._request.options[self._selected_index]
        custom_text = ""
        if opt.is_other:
            other_input = self.query_one("#q-other-input", Input)
            custom_text = other_input.value.strip()
        result = question_result_from_option(
            self._request,
            self._selected_index,
            custom_text=custom_text,
            metadata={"ui": "tui"},
        )
        self.app._on_question_result(result.to_dict())

    def _cancel(self) -> None:
        self.app._on_question_result(None)
