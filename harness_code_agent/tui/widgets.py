"""Textual widgets for the Harness Code Agent TUI."""
from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Input, RichLog, Static, TextArea

from .state import TranscriptBlock

if TYPE_CHECKING:
    from .commands import SlashCommandRegistry
    from .state import PlanStep, SessionStatusSnapshot


# ── SubmitTextArea ──────────────────────────────────────────────────────────

class InputSubmit(Message):
    """Message posted when user presses Enter to submit input."""
    pass


class PaletteComplete(Message):
    """Message posted when user accepts the highlighted completion."""
    pass


class PaletteDismiss(Message):
    """Message posted when user closes the completion palette."""
    pass


class SubmitTextArea(TextArea):
    """TextArea that submits on Enter and inserts newline on Shift+Enter."""

    BINDINGS = TextArea.BINDINGS + [
        Binding("enter", "submit", "Submit", show=False, priority=True),
        Binding("shift+enter", "newline", "Newline", show=False, priority=True),
        Binding("tab", "complete", "Complete", show=False, priority=True),
        Binding("escape", "dismiss_palette", "Dismiss completions", show=False, priority=True),
    ]

    def action_submit(self) -> None:
        """Post a submit message that InputArea handles."""
        if self._route_active_panel_key("enter"):
            return
        self.post_message(InputSubmit())

    def action_newline(self) -> None:
        """Insert a newline at the cursor."""
        if self._route_active_panel_key("enter"):
            return
        self.insert("\n")

    def action_complete(self) -> None:
        """Ask the parent input area to accept the active completion."""
        if self._route_active_panel_key("tab"):
            return
        self.post_message(PaletteComplete())

    def action_dismiss_palette(self) -> None:
        """Ask the parent input area to hide completions."""
        if self._route_active_panel_key("escape"):
            return
        self.post_message(PaletteDismiss())

    def on_key(self, event) -> None:
        if self._route_active_panel_key(event.key):
            event.prevent_default()
            event.stop()

    def _route_active_panel_key(self, key: str) -> bool:
        app = getattr(self, "app", None)
        if app is None or getattr(app, "_active_panel", None) is None:
            return False
        return bool(app.route_active_panel_key(key))

# ── TranscriptView ──────────────────────────────────────────────────────────

_KIND_BORDER_COLORS = {
    "user": "#3874cb",
    "assistant": "#4caf50",
    "tool": "#d79921",
    "thought": "#b48ead",
    "failure": "#bf616a",
    "approval": "#b16286",
    "plan": "#3874cb",
    "summary": "#4caf50",
    "profile": "#3874cb",
    "file": "#d79921",
    "session": "#555555",
    "status": "#555555",
}

_ASSISTANT_MARKDOWN_CHAR_LIMIT = 12_000
_ASSISTANT_DISPLAY_HEAD_CHARS = 4_000
_ASSISTANT_DISPLAY_TAIL_CHARS = 4_000
_ASSISTANT_STREAM_TAIL_CHARS = 12_000


def block_to_rich(block: TranscriptBlock):
    """Convert a TranscriptBlock to a Rich renderable."""
    border = _KIND_BORDER_COLORS.get(block.kind, "#555555")
    status = f" [{block.status}]" if block.status else ""

    if block.kind == "user":
        header = Text(f"You{status}", style=f"bold {border}")
        body = Text(block.body)
        return Group(header, body, Text(""))
    if block.kind == "assistant":
        header = Text(f"Assistant{status}", style=f"bold {border}")
        body = _assistant_body_renderable(block.body, streaming=block.status == "streaming")
        return Group(header, body, Text(""))
    if block.kind == "tool":
        marker = "tool"
        if block.status == "success":
            marker = "ok"
        elif block.status == "failed":
            marker = "error"
        elif block.status == "running":
            marker = "run"
        text = Text()
        text.append(f"  {marker} ", style=f"bold {border}")
        text.append(block.title, style=f"bold {border}")
        if block.body:
            text.append(f"  {block.body}", style="dim")
        return text
    if block.kind == "thought":
        return Text(f"  thinking {block.body or ''}", style=f"dim {border}")
    if block.kind == "failure":
        title = f"❌ Error{status}"
        return Panel(Text(block.body or ""), title=title, border_style=border)
    if block.kind == "approval":
        title = f"Approval{status}"
        return Panel(Text(block.body or ""), title=title, border_style=border)
    if block.kind == "plan":
        title = f"Plan{status}"
        return Panel(Text(block.body or ""), title=title, border_style=border)
    if block.kind == "summary":
        title = f"Turn Summary{status}"
        return Panel(Markdown(block.body) if block.body else Text(""), title=title, border_style=border)
    if block.kind == "profile":
        title = f"Profile{status}"
        return Panel(Text(block.body or ""), title=title, border_style=border)
    if block.kind == "file":
        title = f"File{status}"
        return Panel(Text(block.body or ""), title=title, border_style=border)
    text = Text()
    text.append(f"  {block.title}{status}", style=f"dim {border}")
    if block.body:
        text.append(f"  {block.body}", style="dim")
    return text


def _assistant_body_renderable(body: str, *, streaming: bool):
    if not body:
        return Text("")
    if streaming:
        return Text(_streaming_body_excerpt(body))
    if len(body) > _ASSISTANT_MARKDOWN_CHAR_LIMIT:
        return Text(_assistant_body_excerpt(body))
    return Markdown(body)


def _assistant_body_excerpt(body: str) -> str:
    head = body[:_ASSISTANT_DISPLAY_HEAD_CHARS].rstrip()
    tail = body[-_ASSISTANT_DISPLAY_TAIL_CHARS:].lstrip()
    omitted = max(0, len(body) - len(head) - len(tail))
    return (
        "[display truncated: showing first "
        f"{len(head)} and last {len(tail)} of {len(body)} chars; "
        "full response is stored in the session]\n\n"
        f"{head}\n\n"
        f"... [omitted {omitted} chars] ...\n\n"
        f"{tail}"
    )


def _streaming_body_excerpt(body: str) -> str:
    if len(body) <= _ASSISTANT_STREAM_TAIL_CHARS:
        return body
    tail = body[-_ASSISTANT_STREAM_TAIL_CHARS:].lstrip()
    omitted = len(body) - len(tail)
    return (
        f"[streaming: showing latest {len(tail)} chars; "
        f"{omitted} earlier chars buffered]\n\n"
        f"{tail}"
    )


def welcome_rich(snapshot: SessionStatusSnapshot) -> Panel:
    """Render the welcome message as a Rich Panel."""
    session_text = snapshot.session_id if snapshot.session_id else "pending"
    lines = [
        f"[bold #3874cb]Harness Code Agent[/] [#888888]Textual TUI[/]",
        f"[#888888]session[/] {session_text}  [#888888]profile[/] {snapshot.profile}",
        f"[#888888]workspace[/] {snapshot.cwd}",
        "",
        "[#888888]Use /help for commands. Ctrl-C cancels current turn.[/]",
    ]
    body = Text.from_markup("\n".join(lines))
    return Panel(body, border_style="#555555")


class TranscriptView(RichLog):
    """Transcript area built on RichLog.

    During streaming responses, text is buffered as an active assistant block.
    The app redraws committed transcript cells plus that active block.
    """

    def __init__(self, **kwargs):
        super().__init__(highlight=True, markup=True, wrap=True, auto_scroll=True, **kwargs)
        self._streaming_parts: list[str] = []
        self._streaming_active: bool = False

    def append_block(self, block: TranscriptBlock) -> None:
        self.write(block_to_rich(block))

    def redraw_blocks(self, blocks: list[TranscriptBlock]) -> None:
        self.clear()
        for block in blocks:
            self.append_block(block)

    def begin_streaming(self) -> None:
        """Start buffering streaming text instead of writing immediately."""
        self._streaming_active = True
        self._streaming_parts = []

    def append_streaming(self, text: str) -> None:
        """Accumulate streaming text in the buffer."""
        self._streaming_parts.append(text)

    def streaming_block(self) -> TranscriptBlock | None:
        """Return the active assistant block without committing it."""
        if self._streaming_active and self._streaming_parts:
            return TranscriptBlock("assistant", "assistant", "".join(self._streaming_parts), "streaming")
        return None

    def flush_streaming(self) -> TranscriptBlock | None:
        """Return the complete streaming response and clear the active block."""
        block = None
        if self._streaming_active and self._streaming_parts:
            block = TranscriptBlock("assistant", "assistant", "".join(self._streaming_parts))
        self._streaming_active = False
        self._streaming_parts = []
        return block

    def show_welcome(self, snapshot: SessionStatusSnapshot) -> None:
        self.write(welcome_rich(snapshot))


# ── PlanPanel ───────────────────────────────────────────────────────────────

class PlanPanel(Static):
    """Persistent compact plan step list."""

    steps: reactive[list["PlanStep"]] = reactive(list)

    def update_steps(self, steps: list["PlanStep"]) -> None:
        self.steps = list(steps)
        self.refresh()

    def render(self) -> Text:
        if not self.steps:
            return Text("No plan yet", style="dim")

        text = Text()
        for index, step in enumerate(self.steps):
            if index:
                text.append("\n")
            if step.status == "completed":
                text.append("✓ ", style="#a3be8c")
                text.append(step.text, style="dim strike")
            elif step.status == "current":
                text.append("→ ", style="bold #ebcb8b")
                text.append(step.text, style="bold #ebcb8b")
            else:
                text.append("○ ", style="dim")
                text.append(step.text, style="dim")
        return text


# ── StatusBar ───────────────────────────────────────────────────────────────

class StatusBar(Static):
    """Bottom status bar showing profile, model, turn, status."""

    profile: reactive[str] = reactive("")
    model: reactive[str] = reactive("")
    turn: reactive[int] = reactive(0)
    status: reactive[str] = reactive("idle")

    def render(self) -> Text:
        status_style = "green" if self.status == "idle" else "yellow" if self.status in ("running", "tool", "thinking") else "red"
        text = Text()
        text.append(f" {self.profile}", style="bold")
        text.append(f" │ ", style="dim")
        text.append(f"model: {self.model}", style="")
        text.append(f" │ ", style="dim")
        text.append(f"T: {self.turn}", style="bold")
        text.append(f" │ ", style="dim")
        text.append(f"{self.status}", style=status_style)
        return text

    def update_from_snapshot(self, snap: SessionStatusSnapshot) -> None:
        self.profile = snap.profile
        self.model = snap.model
        self.turn = snap.turn
        self.status = snap.status


# ── ContextBar ──────────────────────────────────────────────────────────────

class ContextBar(Static):
    """Context progress bar and clickable permission mode indicator."""

    context_percent: reactive[int] = reactive(0)
    permission_mode: reactive[str] = reactive("workspace-write")
    token_label: reactive[str] = reactive("0K/0K")
    compact_percent: reactive[int] = reactive(85)

    def render(self) -> Text:
        pct = self.context_percent
        bar_width = 20
        filled = int(bar_width * min(pct, 100) / 100)
        bar = "▓" * filled + "░" * (bar_width - filled)

        if pct >= self.compact_percent:
            bar_color = "#bf616a"
        elif pct >= max(0, self.compact_percent - 10):
            bar_color = "#ebcb8b"
        else:
            bar_color = "#a3be8c"

        if self.permission_mode == "danger-full-access":
            perm_color = "#bf616a"
        elif self.permission_mode == "llm-auto":
            perm_color = "#ebcb8b"
        else:
            perm_color = "#a3be8c"

        text = Text()
        text.append(f" context ", style="dim")
        text.append(bar, style=bar_color)
        text.append(f" {pct}%", style=f"bold {bar_color}")
        text.append(f" {self.token_label}", style="dim")
        text.append(f" auto-compact @{self.compact_percent}%", style="dim")
        text.append(f" │ ", style="dim")
        text.append(f"mode: {self.permission_mode}", style=f"bold {perm_color}")
        text.append(" (click)", style="dim")
        return text

    def update_from_snapshot(self, snap: SessionStatusSnapshot) -> None:
        if snap.context_window_tokens > 0:
            self.context_percent = min(999, int(round(snap.context_tokens * 100 / snap.context_window_tokens)))
        else:
            self.context_percent = 0
        self.token_label = f"{snap.context_tokens // 1000}K/{snap.context_window_tokens // 1000}K"
        self.permission_mode = snap.permission_mode
        if snap.context_window_tokens > 0 and snap.context_compact_threshold > 0:
            self.compact_percent = int(round(snap.context_compact_threshold * 100 / snap.context_window_tokens))
        else:
            self.compact_percent = 85

    def on_click(self) -> None:
        app = getattr(self, "app", None)
        if app is not None and hasattr(app, "action_toggle_permission"):
            app.action_toggle_permission()


# ── CommandPalette ──────────────────────────────────────────────────────────

class CommandPalette(Static):
    """Dropdown completion list for slash commands and @mentions."""

    candidates: reactive[list[tuple[str, str]]] = reactive(list)
    selected_index: reactive[int] = reactive(0)

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)

    def update_candidates(self, candidates: list[tuple[str, str]]) -> None:
        self.candidates = candidates
        self.selected_index = 0
        self._render_palette()

    def _render_palette(self) -> None:
        if not self.candidates:
            self.display = False
            self.update("")
            return
        self.display = True
        lines = []
        for i, (name, desc) in enumerate(self.candidates):
            marker = "▶" if i == self.selected_index else " "
            lines.append(f"{marker} {name}  {desc}")
        self.update("\n".join(lines))

    def move_up(self) -> None:
        if self.candidates:
            self.selected_index = (self.selected_index - 1) % len(self.candidates)
            self._render_palette()

    def move_down(self) -> None:
        if self.candidates:
            self.selected_index = (self.selected_index + 1) % len(self.candidates)
            self._render_palette()

    def get_selected(self) -> str | None:
        if self.candidates and 0 <= self.selected_index < len(self.candidates):
            return self.candidates[self.selected_index][0]
        return None


# ── InputArea ───────────────────────────────────────────────────────────────

class InputArea(Vertical):
    """Input area with TextArea and command palette dropdown."""

    def __init__(self, registry: SlashCommandRegistry | None = None, **kwargs):
        super().__init__(**kwargs)
        self._registry = registry
        self._session = None

    def set_session(self, session) -> None:
        self._session = session

    def compose(self) -> ComposeResult:
        yield CommandPalette(id="cmd-palette")
        yield SubmitTextArea.code_editor(
            id="input-text",
        )

    def on_mount(self) -> None:
        self.query_one("#cmd-palette", CommandPalette).display = False

    def on_key(self, event) -> None:
        """Handle keys for palette navigation and selection."""
        palette = self.query_one("#cmd-palette", CommandPalette)

        # Arrow keys for palette navigation
        if event.key == "up" and palette.display and palette.candidates:
            palette.move_up()
            event.prevent_default()
            return
        if event.key == "down" and palette.display and palette.candidates:
            palette.move_down()
            event.prevent_default()
            return

        # Tab selects the highlighted candidate when palette is open
        if event.key == "tab" and palette.display and palette.candidates:
            selected = palette.get_selected()
            if selected:
                text_area = self.query_one("#input-text", SubmitTextArea)
                if selected.startswith("/") or selected.startswith("@"):
                    text_area.text = _complete_input_text(text_area.text, selected)
                palette.update_candidates([])
                event.prevent_default()
                return

        # Escape closes palette
        if event.key == "escape" and palette.display:
            palette.update_candidates([])
            event.prevent_default()
            return

    @on(InputSubmit)
    def _on_submit(self, event: InputSubmit) -> None:
        """Handle Enter key submission from SubmitTextArea."""
        palette = self.query_one("#cmd-palette", CommandPalette)
        text_area = self.query_one("#input-text", SubmitTextArea)
        current_text = text_area.text.strip()
        if (
            palette.display
            and current_text.startswith("/")
            and self._registry is not None
            and any(current_text in spec.names() for spec in self._registry.candidates())
        ):
            palette.update_candidates([])
            self._submit_input()
            return
        # If palette is open, select the candidate instead of submitting
        if palette.display and palette.candidates:
            selected = palette.get_selected()
            if selected:
                if selected.startswith("/") or selected.startswith("@"):
                    text_area.text = _complete_input_text(text_area.text, selected)
                palette.update_candidates([])
                return
        # Otherwise submit
        self._submit_input()

    @on(PaletteComplete)
    def _on_complete(self, event: PaletteComplete) -> None:
        palette = self.query_one("#cmd-palette", CommandPalette)
        if not palette.display or not palette.candidates:
            return
        selected = palette.get_selected()
        if selected:
            text_area = self.query_one("#input-text", SubmitTextArea)
            if selected.startswith("/") or selected.startswith("@"):
                text_area.text = _complete_input_text(text_area.text, selected)
            palette.update_candidates([])
            event.stop()

    @on(PaletteDismiss)
    def _on_dismiss_palette(self, event: PaletteDismiss) -> None:
        palette = self.query_one("#cmd-palette", CommandPalette)
        if palette.display:
            palette.update_candidates([])
            event.stop()

    def _submit_input(self) -> None:
        """Submit the current input text to the app."""
        text_area = self.query_one("#input-text", TextArea)
        text = text_area.text.strip()
        if not text:
            return

        # Delegate to app for actual submission
        app = self.app
        if app is None:
            return

        # Handle slash commands
        if text.startswith("/"):
            try:
                should_continue = app.session.handle_slash_command(text)
            except Exception:
                return
            if not should_continue:
                app.exit()
            return

        # Submit to agent
        if app._submit_async(text):
            text_area.text = ""
            self.query_one("#cmd-palette", CommandPalette).update_candidates([])

    @on(TextArea.Changed, "#input-text")
    def _on_text_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        self._update_completions(text)

    def _update_completions(self, text: str) -> None:
        palette = self.query_one("#cmd-palette", CommandPalette)
        stripped = text.lstrip()

        if stripped.startswith("/") and " " not in stripped:
            if self._registry:
                from .completion import fuzzy_command_candidates
                candidates = [
                    (spec.name, f"{spec.group} - {spec.description}")
                    for spec in fuzzy_command_candidates(self._registry, stripped)
                ]
                palette.update_candidates(candidates)
            return

        from .completion import current_mention_query, mention_candidates
        mention = current_mention_query(text)
        if mention is not None and self._session:
            prefix, _start = mention
            skill_registry = getattr(self._session, "skill_registry", None)
            candidates = [
                (c.display, c.description)
                for c in mention_candidates(
                    self._session.cwd,
                    prefix,
                    self._session.session_store,
                    skill_catalog=getattr(skill_registry, "catalog", None),
                )
            ]
            palette.update_candidates(candidates)
            return

        palette.update_candidates([])

    def get_text(self) -> str:
        return self.query_one("#input-text", TextArea).text

    def clear(self) -> None:
        self.query_one("#input-text", TextArea).text = ""
        self.query_one("#cmd-palette", CommandPalette).update_candidates([])

    def focus_input(self) -> None:
        self.query_one("#input-text", TextArea).focus()


def _complete_input_text(current_text: str, selected: str) -> str:
    if selected.startswith("@"):
        from .completion import replace_mention_fragment

        return replace_mention_fragment(current_text, selected.removeprefix("@"))
    return selected + " "
