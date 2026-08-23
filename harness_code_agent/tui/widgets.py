"""Textual widgets for the VeriForge TUI."""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widgets import RichLog, Static, TextArea

from .state import TranscriptBlock

if TYPE_CHECKING:
    from .commands import SlashCommandRegistry
    from .state import SessionStatusSnapshot


# ── SubmitTextArea ──────────────────────────────────────────────────────────

class InputSubmit(Message):
    """Message posted when user presses Enter to submit input."""


class PaletteComplete(Message):
    """Message posted when user accepts the highlighted completion."""


class PaletteDismiss(Message):
    """Message posted when user closes the completion palette."""


class SubmitTextArea(TextArea):
    """TextArea that submits on Enter and inserts newline on Shift+Enter."""

    PLACEHOLDER = "输入任务  ·  / 命令  ·  @ 文件"

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

    def render_line(self, y: int) -> Strip:
        """Render a visible first-line prompt without displacing the cursor."""
        line = super().render_line(y)
        if y != 0 or self.text:
            return line

        available = max(0, line.cell_length - 1)
        if available <= 0:
            return line
        ghost_style = Style(
            color="#a3a3a3",
            bgcolor=self.rich_style.bgcolor,
        )
        ghost_strip = Strip([Segment(f" {self.PLACEHOLDER}", ghost_style)])
        ghost_strip = ghost_strip.crop(0, available).extend_cell_length(
            available,
            ghost_style,
        )
        return Strip.join([line.crop(0, 1), ghost_strip])

# ── TranscriptView ──────────────────────────────────────────────────────────

_INK = "#f5f5f5"
_MUTED = "#b4b4b8"
_SUBTLE = "#737373"
_ACCENT = "#7dd3fc"
_SUCCESS = "#86efac"
_WARNING = "#fde68a"
_ERROR = "#fca5a5"

_BLOCK_LABELS = {
    "failure": "错误",
    "error": "错误",
    "file changed": "文件已变更",
    "profile switched": "配置已切换",
    "profile route": "配置路由",
    "plan ready": "计划已准备",
    "context compacted": "上下文已压缩",
    "turn cancelled": "回合已取消",
    "startup failed": "启动失败",
    "queued": "已排队",
    "queued while starting": "启动后排队",
    "output": "输出",
}

_ASSISTANT_MARKDOWN_CHAR_LIMIT = 12_000
_ASSISTANT_DISPLAY_HEAD_CHARS = 4_000
_ASSISTANT_DISPLAY_TAIL_CHARS = 4_000
_ASSISTANT_STREAM_TAIL_CHARS = 12_000


def block_to_rich(block: TranscriptBlock):
    """Convert a TranscriptBlock to a Rich renderable."""
    if block.kind == "user":
        header = Text("› 你", style=f"bold {_ACCENT}")
        body = Text(block.body, style=_INK)
        return Group(header, body, Text(""))
    if block.kind == "assistant":
        header = Text("• 助手", style=f"bold {_SUCCESS}")
        body = _assistant_body_renderable(block.body, streaming=block.status == "streaming")
        return Group(header, body, Text(""))
    if block.kind == "tool":
        return _tool_line(block)
    if block.kind == "middleware":
        return _middleware_line(block)
    if block.kind == "thought":
        return Text(f"  · 思考 {block.body}", style=f"italic {_SUBTLE}")
    if block.kind == "failure":
        return Panel(
            Text(block.body or "", style=_INK),
            title=Text(_BLOCK_LABELS.get(block.title, block.title or "错误"), style=f"bold {_ERROR}"),
            border_style=_ERROR,
        )
    if block.kind == "plan":
        return _plan_update_renderable(block.body)
    if block.kind == "file":
        header = Text()
        header.append("  ✎ ", style=f"bold {_WARNING}")
        header.append(_BLOCK_LABELS.get(block.title, block.title or "文件已变更"), style=f"bold {_MUTED}")
        if not block.body:
            return header
        return Group(header, _diff_renderable(block.body), Text(""))
    if block.kind == "profile":
        line = Text()
        line.append("  ⇄ ", style=f"bold {_ACCENT}")
        line.append(block.title, style=_MUTED)
        if block.body:
            line.append(f"  {block.body}", style=_MUTED)
        return line
    text = Text()
    text.append(f"  {block.title}", style=_MUTED)
    if block.body:
        text.append(f"  {block.body}", style=_SUBTLE)
    return text


def _diff_renderable(diff_text: str) -> Text:
    """Colorize a unified diff: red removals, green additions, dim context."""
    text = Text()
    lines = diff_text.splitlines()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        if line.startswith("+"):
            text.append(line, style="green")
        elif line.startswith("-"):
            text.append(line, style=_ERROR)
        elif line.startswith(("@@", "…")):
            text.append(line, style=_SUBTLE)
        else:
            text.append(line, style=_MUTED)
    return text


def _tool_line(block: TranscriptBlock) -> Text:
    marker = "·"
    marker_color = _MUTED
    if block.status == "success":
        marker = "✓"
        marker_color = _SUCCESS
    elif block.status == "failed":
        marker = "×"
        marker_color = _ERROR
    elif block.status == "running":
        marker = "›"
        marker_color = _ACCENT
    text = Text()
    text.append(f"  {marker} ", style=f"bold {marker_color}")
    text.append(_humanize_tool_title(block.title, block.status), style=f"bold {_INK}")
    if block.body:
        first, _, rest = block.body.partition("\n")
        text.append(f"  {first}", style=_SUBTLE)
        if rest:
            text.append("\n")
            text.append(f"    {rest}", style=f"italic {_ERROR}")
    return text


def _middleware_line(block: TranscriptBlock) -> Text:
    marker = "◇"
    color = _ACCENT
    outcome = "已通过"
    if block.status == "guided":
        marker = "◆"
        color = _WARNING
        outcome = "已引导"
    elif block.status == "blocked":
        marker = "×"
        color = _ERROR
        outcome = "已阻止"
    text = Text()
    text.append(f"  {marker} ", style=f"bold {color}")
    text.append(block.title, style=f"bold {color}")
    text.append(f"  {outcome}", style=color)
    if block.body:
            text.append(f"  ·  {block.body}", style=_SUBTLE)
    return text


def _humanize_tool_title(title: str, status: str) -> str:
    """Turn internal tool identifiers into a compact activity feed label."""
    tool, separator, details = title.partition("(")
    details = details[:-1] if separator and details.endswith(")") else details
    labels = {
        "read_file": ("读取", "已读取"),
        "write_file": ("写入", "已写入"),
        "apply_patch": ("编辑", "已编辑"),
        "run_bash": ("执行", "已执行"),
        "search_files": ("搜索", "已搜索"),
        "list_files": ("列出", "已列出"),
        "parallel_commands": ("并行执行", "已并行执行"),
        "parallel_agents": ("并行委派", "已并行委派"),
        "delegate_agent": ("委派", "已委派"),
        "update_plan_state": ("更新计划", "计划已更新"),
    }
    running, completed = labels.get(tool, (tool.replace("_", " "), tool.replace("_", " ")))
    label = running if status == "running" else completed
    if details:
        return f"{label}  {details}"
    return label


def _plan_update_renderable(body: str) -> Group:
    lines = Text()
    for index, raw_line in enumerate(body.splitlines()):
        if index:
            lines.append("\n")
        marker, _, label = raw_line.partition(" ")
        if marker == "✓":
            lines.append("  ✓ ", style=f"bold {_SUCCESS}")
            lines.append(label, style=f"strike {_MUTED}")
        elif marker == "›":
            lines.append("  › ", style=f"bold {_WARNING}")
            lines.append(label, style=f"bold {_INK}")
        else:
            lines.append("  ○ ", style=_MUTED)
            lines.append(label, style=_MUTED)
    return Group(Text("计划", style=f"bold {_ACCENT}"), lines, Text(""))


def _assistant_body_renderable(body: str, *, streaming: bool):
    if not body:
        return Text("", style=_INK)
    if streaming:
        return Text(_streaming_body_excerpt(body), style=_INK)
    if len(body) > _ASSISTANT_MARKDOWN_CHAR_LIMIT:
        return Text(_assistant_body_excerpt(body), style=_INK)
    return Markdown(body, style=_INK)


def _assistant_body_excerpt(body: str) -> str:
    head = body[:_ASSISTANT_DISPLAY_HEAD_CHARS].rstrip()
    tail = body[-_ASSISTANT_DISPLAY_TAIL_CHARS:].lstrip()
    omitted = max(0, len(body) - len(head) - len(tail))
    return (
        f"[响应过长：显示开头 {len(head)} 字符和结尾 {len(tail)} 字符，"
        f"共 {len(body)} 字符]\n\n"
        f"{head}\n\n"
        f"... [中间省略 {omitted} 字符] ...\n\n"
        f"{tail}"
    )


def _streaming_body_excerpt(body: str) -> str:
    if len(body) <= _ASSISTANT_STREAM_TAIL_CHARS:
        return body
    tail = body[-_ASSISTANT_STREAM_TAIL_CHARS:].lstrip()
    len(body) - len(tail)
    return f"[显示最近 {len(tail)} / {len(body)} 字符]\n\n{tail}"


_WELCOME_KEY_HINTS = "  / 命令  ·  @ 文件  ·  Ctrl+O 可观测性  ·  Ctrl+C 中断"


def welcome_rich(snapshot: SessionStatusSnapshot) -> Group:
    """Render a quiet, terminal-native arrival state."""
    brand = Text("VeriForge", style=f"bold {_INK}")
    meta = Text()
    meta.append(snapshot.profile, style=f"bold {_ACCENT}")
    meta.append("  ·  ", style=_SUBTLE)
    permission_color = _ERROR if snapshot.permission_mode == "danger-full-access" else _MUTED
    meta.append(snapshot.permission_mode, style=permission_color)
    meta.append("  ·  ", style=_SUBTLE)
    workspace_path = Path(snapshot.cwd)
    workspace_name = workspace_path.name or str(workspace_path)
    meta.append(workspace_name, style=_MUTED)
    if snapshot.model:
        meta.append("  ·  ", style=_SUBTLE)
        meta.append(snapshot.model, style=_MUTED)
    hints = Text(_WELCOME_KEY_HINTS, style=_SUBTLE)
    return Group(brand, meta, hints, Text(""))


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


# ── StatusBar ───────────────────────────────────────────────────────────────

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_STATUS_LABELS = {
    "starting": "启动中",
    "loading history": "加载历史会话",
    "history loaded": "历史会话已加载",
    "ready": "就绪",
    "pending": "等待任务",
    "running": "处理中",
    "running command": "执行命令",
    "queued": "已排队",
    "queued while starting": "启动后排队",
    "tool": "执行工具",
    "thinking": "模型思考中",
    "compacting context": "压缩上下文",
    "connecting external tools": "连接外部工具",
    "needs attention": "需要处理",
    "plan ready": "计划已准备",
    "blocked": "已停止",
    "failed": "失败",
    "idle": "空闲",
}

_PERMISSION_LABELS = {
    "workspace-write": "工作区可写",
    "danger-full-access": "完全访问",
    "llm-auto": "模型自动确认",
}


class StatusBar(Static):
    """Bottom status bar: activity spinner, profile, context budget, model."""

    profile: reactive[str] = reactive("")
    model: reactive[str] = reactive("")
    turn: reactive[int] = reactive(0)
    status: reactive[str] = reactive("idle")
    spinner_frame: reactive[int] = reactive(0)
    elapsed_seconds: reactive[int] = reactive(0)
    context_percent: reactive[int] = reactive(0)
    permission_mode: reactive[str] = reactive("workspace-write")
    dirty_count: reactive[int] = reactive(0)

    def render(self) -> Text:
        active = self.status not in {"idle", "ready"}
        text = Text()
        if active:
            frame = _SPINNER_FRAMES[self.spinner_frame % len(_SPINNER_FRAMES)]
            label = _STATUS_LABELS.get(self.status, self.status)
            busy_style = (
                _ERROR
                if self.status in {"needs attention", "blocked", "failed"}
                else _WARNING
            )
            text.append(f" {frame} ", style=f"bold {busy_style}")
            text.append(label, style=f"bold {busy_style}")
            if self.elapsed_seconds:
                minutes, seconds = divmod(self.elapsed_seconds, 60)
                elapsed = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
                text.append(f"  {elapsed}", style=_SUBTLE)
            text.append("  ·  ", style=_SUBTLE)
        else:
            text.append(" ")
        text.append(self.profile, style=f"bold {_ACCENT}")
        text.append("  ·  ", style=_SUBTLE)
        permission_label = _PERMISSION_LABELS.get(self.permission_mode, self.permission_mode)
        permission_color = _ERROR if self.permission_mode == "danger-full-access" else _MUTED
        text.append(permission_label, style=permission_color)
        text.append("  ·  ", style=_SUBTLE)
        remaining = max(0, 100 - self.context_percent)
        context_style = _MUTED
        if remaining <= 20:
            context_style = _ERROR
        elif remaining <= 40:
            context_style = _WARNING
        text.append(f"剩余上下文 {remaining}%", style=context_style)
        if self.dirty_count and (not self.size.width or self.size.width >= 70):
            text.append(f"  ·  已变更 {self.dirty_count} 项", style=_WARNING)
        if self.model:
            text.append("  ·  ", style=_SUBTLE)
            text.append(self.model, style=f"bold {_INK}")
        return text

    def advance_spinner(self) -> None:
        if self.status not in {"idle", "ready"}:
            self.spinner_frame = self.spinner_frame + 1
            now = time.monotonic()
            active_status = getattr(self, "_active_status", None)
            if active_status != self.status:
                self._active_status = self.status
                self._active_since = now
                self.elapsed_seconds = 0
            else:
                self.elapsed_seconds = int(now - self._active_since)
        else:
            self._active_status = self.status
            self._active_since = time.monotonic()
            self.elapsed_seconds = 0

    def update_from_snapshot(self, snap: SessionStatusSnapshot) -> None:
        self.profile = snap.profile
        self.model = snap.model
        self.turn = snap.turn
        self.status = snap.status
        self.permission_mode = snap.permission_mode
        self.dirty_count = snap.dirty_count
        if snap.context_window_tokens > 0:
            self.context_percent = min(999, round(snap.context_tokens * 100 / snap.context_window_tokens))
        else:
            self.context_percent = 0

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
        text = Text()
        for i, (name, desc) in enumerate(self.candidates):
            if i:
                text.append("\n")
            if i == self.selected_index:
                text.append(f" {name}", style=f"bold reverse {_INK}")
                if desc:
                    text.append(f"  {desc}", style=_SUBTLE)
            else:
                text.append(f" {name}", style=f"bold {_ACCENT}")
                if desc:
                    text.append(f"  {desc}", style=_SUBTLE)
        self.update(text)

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

    def set_registry(self, registry: SlashCommandRegistry) -> None:
        self._registry = registry

    def compose(self) -> ComposeResult:
        yield CommandPalette(id="cmd-palette")
        with Horizontal(id="prompt-row"):
            yield Static("›", id="input-prompt")
            yield SubmitTextArea(
                id="input-text",
                soft_wrap=True,
                show_line_numbers=False,
                tab_behavior="focus",
            )

    def on_mount(self) -> None:
        self.query_one("#cmd-palette", CommandPalette).display = False
        self._resize_composer(self.query_one("#input-text", SubmitTextArea))

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
                if selected.startswith(("/", "@")):
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
                if selected.startswith(("/", "@")):
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
            if selected.startswith(("/", "@")):
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

        if app._submit_async(text):
            text_area.text = ""
            self.query_one("#cmd-palette", CommandPalette).update_candidates([])

    @on(TextArea.Changed, "#input-text")
    def _on_text_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        self._resize_composer(event.text_area)
        self._update_completions(text)

    def _resize_composer(self, text_area: TextArea) -> None:
        """Keep a roomy composer and grow it for longer multiline prompts."""
        line_count = max(3, min(7, text_area.text.count("\n") + 2))
        text_area.styles.height = line_count
        prompt_row = self.query_one("#prompt-row")
        prompt_row.styles.height = line_count
        self.query_one("#input-prompt").styles.height = line_count

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
            candidates = [
                (c.display, c.description)
                for c in mention_candidates(
                    self._session.cwd,
                    prefix,
                    self._session.session_store,
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
