"""Textual-based TUI for VeriForge."""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import TextArea

log = logging.getLogger("harness.tui")

_STREAM_FLUSH_DELAY_SECONDS = 0.005
_STREAM_FLUSH_MAX_CHARS = 4_096
_CONTEXT_SNAPSHOT_MIN_INTERVAL_SECONDS = 1.0
_SPINNER_INTERVAL_SECONDS = 0.12
_STARTUP_STAGE_LABELS = {
    "starting": "启动中",
    "loading profile": "加载配置",
    "loading history": "加载历史会话",
    "history loaded": "历史会话已加载",
    "loading skills": "加载技能",
    "loading workspace": "加载工作区",
    "connecting external tools": "连接外部工具",
    "external tools ready": "外部工具已就绪",
    "ready": "就绪",
}

from .. import config
from ..agent.cancellation import CancellationToken, CancelledError
from ..core.mentions import MentionResolutionError
from .approval import TuiApprovalProvider
from .commands import default_command_registry
from .question import TuiQuestionProvider
from .screens import ApprovalResult, QuestionResult
from .state import SessionStatusSnapshot, TranscriptBlock, TuiState
from .widgets import (
    InputArea,
    StatusBar,
    TranscriptView,
)


def InteractiveSession(**kwargs):
    """Import the orchestration stack only inside the startup worker."""
    from ..core.interactive import InteractiveSession as Session

    return Session(**kwargs)

# ── Messages (worker → UI thread) ──────────────────────────────────────────

class StreamDelta(Message):
    """A streaming text fragment from the agent."""
    def __init__(self, delta: str) -> None:
        super().__init__()
        self.delta = delta


class SessionEvent(Message):
    """A session event from InteractiveSession."""
    def __init__(self, event: object) -> None:
        super().__init__()
        self.event = event


class SubmitComplete(Message):
    """Submit finished successfully."""
    def __init__(self, result: object) -> None:
        super().__init__()
        self.result = result


class SubmitCancelled(Message):
    def __init__(self) -> None:
        super().__init__()


class SubmitError(Message):
    """Submit failed with an error."""
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class SlashCommandComplete(Message):
    """A local slash command finished outside the UI thread."""

    def __init__(self, should_continue: bool):
        super().__init__()
        self.should_continue = should_continue


class SessionReady(Message):
    """The background startup worker finished creating the session."""

    def __init__(self, session):
        super().__init__()
        self.session = session


class SessionStartupError(Message):
    """The background startup worker could not create the session."""

    def __init__(self, error: str):
        super().__init__()
        self.error = error


class StartupProgress(Message):
    """A user-visible stage from background session initialization."""

    def __init__(self, stage: str):
        super().__init__()
        self.stage = stage


class ContextSnapshotReady(Message):
    """A background context-token snapshot ready for the status bar."""

    def __init__(self, session, context_tokens: int, context_window_tokens: int) -> None:
        super().__init__()
        self.session = session
        self.context_tokens = context_tokens
        self.context_window_tokens = context_window_tokens


class OutputEvent(Message):
    """Output a status block to the transcript from a worker thread."""
    def __init__(self, text: str, title: str = "output") -> None:
        super().__init__()
        self.text = text
        self.title = title


# ── TuiApp ──────────────────────────────────────────────────────────────────

class TuiApp(App):
    """Full-screen Textual TUI for VeriForge."""

    TITLE = "VeriForge"
    SUB_TITLE = "本地代码工作区"

    BINDINGS: ClassVar[list] = [
        Binding("ctrl+c", "cancel", "Cancel", show=False, priority=True),
        Binding("ctrl+o", "observe", "Observe", show=False, priority=True),
        # Panel keys: check_action guards these to only fire when a panel is active.
        # priority=True ensures they are checked before widget-level handlers so
        # panel input works even if focus has drifted from the panel widget.
        Binding("enter", "panel_key('enter')", "", show=False, priority=True),
        Binding("escape", "panel_key('escape')", "", show=False, priority=True),
        Binding("1", "panel_key('1')", "", show=False, priority=True),
        Binding("2", "panel_key('2')", "", show=False, priority=True),
        Binding("3", "panel_key('3')", "", show=False, priority=True),
        Binding("4", "panel_key('4')", "", show=False, priority=True),
        Binding("5", "panel_key('5')", "", show=False, priority=True),
        Binding("6", "panel_key('6')", "", show=False, priority=True),
        Binding("7", "panel_key('7')", "", show=False, priority=True),
        Binding("8", "panel_key('8')", "", show=False, priority=True),
        Binding("9", "panel_key('9')", "", show=False, priority=True),
        Binding("up", "panel_key('up')", "", show=False, priority=True),
        Binding("down", "panel_key('down')", "", show=False, priority=True),
        Binding("left", "panel_key('left')", "", show=False, priority=True),
        Binding("right", "panel_key('right')", "", show=False, priority=True),
    ]

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
        color: $text;
    }

    #transcript {
        width: 1fr;
        height: 1fr;
        padding: 1 1;
        background: $background;
        overflow-x: hidden;
        scrollbar-size-horizontal: 0;
        scrollbar-size-vertical: 1;
    }

    #input-area {
        height: auto;
        max-height: 8;
        margin: 0 1;
        border: none;
        background: $surface;
    }

    #input-area:focus-within {
        background: $panel;
    }

    #prompt-row {
        height: 3;
    }

    #input-prompt {
        width: 3;
        height: 3;
        padding: 1 0 0 1;
        color: $text;
        text-style: bold;
    }

    #cmd-palette {
        display: none;
        height: auto;
        max-height: 10;
        background: $panel;
        border-bottom: solid $border-blurred;
        padding: 0 1;
    }

    #input-text {
        height: 3;
        width: 1fr;
        background: $surface;
        color: $text;
        border: none;
        padding: 1 1 0 1;
        overflow-x: hidden;
        scrollbar-size-horizontal: 0;
    }

    #input-text:focus {
        background: $panel;
    }

    #status-bar {
        height: 1;
        margin: 0 1;
        background: $background;
        color: $text-muted;
    }

    /* Approval panel */
    .approval-panel {
        height: auto;
        border: solid $accent;
        padding: 1;
    }

    .approval-panel Static.body {
        height: auto;
    }

    .approval-panel Static.choices {
        height: 1;
        margin-top: 1;
    }

    /* Question panel */
    .question-panel {
        height: auto;
        border: solid $primary;
        padding: 1;
    }

    .question-panel Static.body {
        height: auto;
    }

    .question-panel Input {
        height: 3;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        cwd: str | Path,
        profile_name: str,
        profile_explicit: bool = False,
        resume_session_id: str | None = None,
        first_task: str = "",
    ):
        super().__init__()
        self.cwd = Path(cwd).resolve()
        self.profile_name = profile_name
        self.profile_explicit = profile_explicit
        self.resume_session_id = resume_session_id
        self.first_task = first_task
        self._session_factory = InteractiveSession
        self.registry = default_command_registry()
        self.session = None
        self.state: TuiState | None = None
        self._startup_error = ""
        self._exiting = False
        self._pending_events: list = []
        self._streaming_current_response = False
        self._stream_header_printed = False
        self._pending_stream_chunks: deque[str] = deque()
        self._pending_stream_chars = 0
        self._stream_flush_scheduled = False
        self._submitting = False
        self._pending_submissions: deque[str] = deque()
        self._cancellation_token = CancellationToken()
        self._last_context_refresh = 0.0
        self._context_refresh_in_flight = False

        # Approval/question state
        self._approval_event: threading.Event | None = None
        self._approval_result_holder: list = []
        self._question_event: threading.Event | None = None
        self._question_result_holder: list = []
        self._active_panel: str | None = None  # "approval" or "question"

    def run(self, *args, **kwargs) -> int:
        """Run the Textual app and preserve the VeriForge CLI exit-code contract."""
        super().run(*args, **kwargs)
        return 0

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "panel_key":
            return self._active_panel is not None
        return True

    def compose(self) -> ComposeResult:
        yield TranscriptView(id="transcript")
        yield InputArea(registry=self.registry, id="input-area")
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        """Paint an interactive shell immediately, then initialize in a worker."""
        permission_mode = os.environ.get("HARNESS_PERMISSION_MODE", "workspace-write")
        self.state = TuiState(
            snapshot=SessionStatusSnapshot(
                profile=self.profile_name,
                model=config.MODEL,
                provider=config.PROVIDER,
                permission_mode=permission_mode,
                session_id=None,
                cwd=self.cwd,
                status="starting",
            )
        )

        input_area = self.query_one("#input-area", InputArea)
        transcript = self.query_one("#transcript", TranscriptView)
        transcript.show_welcome(self.state.snapshot)
        startup_block = TranscriptBlock(
            "status",
            "starting",
            "正在准备工作区并连接已配置工具…",
            "running",
        )
        self.state.add_block(startup_block)
        transcript.append_block(startup_block)
        self._refresh_bars()
        self.set_interval(_SPINNER_INTERVAL_SECONDS, self._advance_spinner)
        input_area.focus_input()

        approval_provider = TuiApprovalProvider(project_root=self.cwd, app_tui=self)
        question_provider = TuiQuestionProvider(app_tui=self)
        self._initialize_session(approval_provider, question_provider)

        if self.first_task:
            self._submit_async(self.first_task)

    @work(thread=True, group="startup", exclusive=True, exit_on_error=False)
    def _initialize_session(self, approval_provider, question_provider) -> None:
        """Build the potentially slow session without blocking the Textual loop."""
        try:
            session = self._session_factory(
                cwd=self.cwd,
                profile_name=self.profile_name,
                profile_explicit=self.profile_explicit,
                resume_session_id=self.resume_session_id,
                stream_sink=self._stream_delta,
                event_listener=self._event_listener,
                approval_provider=approval_provider,
                question_provider=question_provider,
                output_sink=self._output,
                enable_turn_summary=False,
                startup_sink=self._startup_progress,
            )
        except Exception as exc:
            self.post_message(SessionStartupError(f"{type(exc).__name__}: {exc}"))
            return

        if self._exiting:
            try:
                session.close()
            except Exception:
                log.debug("Error closing a session that finished after TUI exit", exc_info=True)
            return
        self.post_message(SessionReady(session))

    def on_startup_progress(self, msg: StartupProgress) -> None:
        if self.state is None or msg.stage == "ready":
            return
        if msg.stage == "external tools ready":
            if self.state.snapshot.status == "connecting external tools":
                self.state.snapshot.status = "running" if self._submitting else "idle"
            self._refresh_bars()
            return
        display_stage = _STARTUP_STAGE_LABELS.get(msg.stage, msg.stage)
        self.state.snapshot.status = msg.stage
        for block in self.state.blocks:
            if block.title == "starting":
                block.body = f"{display_stage}…"
                break
        self._redraw_transcript()
        self._refresh_bars()

    def on_session_ready(self, msg: SessionReady) -> None:
        """Attach a ready session and drain input accepted during startup."""
        self.session = msg.session
        display_profile = getattr(self.session, "display_profile", None)
        if not display_profile:
            display_profile = self.session.profile.name()
        session_id = getattr(
            self.session,
            "session_id",
            getattr(getattr(self.session, "session", None), "id", None),
        )
        is_bound = getattr(self.session, "is_bound", True)
        self.state.snapshot.profile = display_profile
        self.state.snapshot.permission_mode = self.session.permission_mode
        self.state.snapshot.session_id = session_id
        self.state.snapshot.cwd = Path(self.session.cwd)
        self.state.snapshot.status = "idle" if is_bound else "pending"

        for event in self._pending_events:
            self._process_session_event(event)
        self._pending_events.clear()

        input_area = self.query_one("#input-area", InputArea)
        self.registry = default_command_registry(
            skill_registry=getattr(self.session, "skill_registry", None)
        )
        input_area.set_registry(self.registry)
        input_area.set_session(self.session)
        self.state.blocks = [block for block in self.state.blocks if block.title != "starting"]
        self._redraw_transcript()
        self._refresh_bars()
        if hasattr(self.session, "warm_mcp_tools"):
            self._warm_mcp_tools()
        self._drain_pending_submissions()

    @work(thread=True, group="mcp-warmup", exclusive=True, exit_on_error=False)
    def _warm_mcp_tools(self) -> None:
        try:
            self.session.warm_mcp_tools()
        except Exception as exc:
            log.warning("MCP warmup failed: %s", exc)

    def on_session_startup_error(self, msg: SessionStartupError) -> None:
        self._startup_error = msg.error
        self.state.snapshot.status = "needs attention"
        self.state.blocks = [block for block in self.state.blocks if block.title != "starting"]
        self._output(msg.error, title="startup failed")
        self._refresh_bars()

    def on_context_snapshot_ready(self, msg: ContextSnapshotReady) -> None:
        """Apply a context snapshot without blocking the UI thread."""
        self._context_refresh_in_flight = False
        if self.state is None or msg.session is not self.session:
            return
        self.state.snapshot.context_tokens = msg.context_tokens
        self.state.snapshot.context_window_tokens = msg.context_window_tokens
        self._last_context_refresh = time.monotonic()
        self._refresh_bars()

    # ── Input handling ──────────────────────────────────────────────────────

    @on(TextArea.Changed, "#input-text")
    def _on_input_changed(self, event: TextArea.Changed) -> None:
        """Update completion palette based on input text."""
        input_area = self.query_one("#input-area", InputArea)
        input_area._update_completions(event.text_area.text)

    def on_key(self, event) -> None:
        """Route keys to active inline panels even if terminal focus drifts."""
        if self.route_active_panel_key(event.key):
            event.prevent_default()
            event.stop()

    def route_active_panel_key(self, key: str) -> bool:
        """Send a key to the active approval/question panel."""
        if self._active_panel == "approval":
            try:
                panel = self.query_one("#approval-panel")
                return bool(panel.handle_key(key))
            except NoMatches:
                log.debug("Approval panel not found while routing key '%s'", key)
                return False
            except Exception:
                log.warning("Error routing key '%s' to approval panel", key, exc_info=True)
                return False
        if self._active_panel == "question":
            try:
                panel = self.query_one("#question-panel")
                return bool(panel.handle_key(key))
            except NoMatches:
                log.debug("Question panel not found while routing key '%s'", key)
                return False
            except Exception:
                log.warning("Error routing key '%s' to question panel", key, exc_info=True)
                return False
        return False

    # ── Async submit ────────────────────────────────────────────────────────

    def _submit_async(self, text: str) -> bool:
        """Accept user input and run it now or enqueue it behind the active turn."""
        text = str(text or "").strip()
        if not text:
            return False
        if self._startup_error:
            self._output(self._startup_error, title="启动失败")
            return False
        if self.session is None:
            self._pending_submissions.append(text)
            self._output(_preview_submission(text), title="启动后排队")
            return True
        if self._submitting:
            self._pending_submissions.append(text)
            self._output(_preview_submission(text), title="已排队")
            return True
        return self._start_submission(text)

    def _start_submission(self, text: str) -> bool:
        """Dispatch one accepted submission. Non-blocking for agent turns."""
        if self._is_local_slash_command(text):
            self._submitting = True
            self.state.snapshot.status = (
                "loading history" if self._is_resume_command(text) else "running command"
            )
            self._refresh_bars()
            self._slash_command_worker(text)
            return True
        self._submitting = True
        self._clear_pending_stream_deltas()
        self._streaming_current_response = False
        self._stream_header_printed = False
        token = CancellationToken()
        self._cancellation_token = token
        self._submit_worker(text, token)
        return True

    @work(thread=True, group="turn", exclusive=True, exit_on_error=False)
    def _submit_worker(self, text: str, cancellation_token: CancellationToken) -> None:
        """Run session.submit() in a background thread."""
        try:
            result = self.session.submit(text, cancellation_token=cancellation_token)
            self.post_message(SubmitComplete(result))
        except CancelledError:
            self.post_message(SubmitCancelled())
        except MentionResolutionError as exc:
            self.post_message(SubmitError(str(exc)))
        except Exception as exc:
            self.post_message(SubmitError(str(exc)))

    def _is_local_slash_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        return not (
            self.registry is not None and self.registry.is_agent_command(text)
        )

    @staticmethod
    def _is_resume_command(text: str) -> bool:
        return str(text or "").strip().split(maxsplit=1)[:1] == ["/resume"]

    @work(thread=True, group="turn", exclusive=True, exit_on_error=False)
    def _slash_command_worker(self, text: str) -> None:
        try:
            should_continue = self.session.handle_slash_command(text)
        except Exception as exc:
            self.post_message(SubmitError(str(exc)))
            return
        self.post_message(SlashCommandComplete(should_continue))

    def on_slash_command_complete(self, msg: SlashCommandComplete) -> None:
        self._submitting = False
        self.state.snapshot.status = "idle"
        self._refresh_bars()
        if not msg.should_continue:
            self.exit()
            return
        self._drain_pending_submissions()

    def _drain_pending_submissions(self) -> None:
        while not self._submitting and self._pending_submissions:
            text = self._pending_submissions.popleft()
            self._start_submission(text)

    # ── Message handlers (UI thread) ────────────────────────────────────────

    def on_stream_delta(self, msg: StreamDelta) -> None:
        """Handle streaming delta from background thread.

        Deltas are coalesced on the UI thread so a burst of tiny chunks does
        not redraw the whole transcript once per chunk.
        """
        if not msg.delta:
            return
        self._streaming_current_response = True
        self._pending_stream_chunks.append(msg.delta)
        self._pending_stream_chars += len(msg.delta)
        self._schedule_stream_flush()

    def on_session_event(self, msg: SessionEvent) -> None:
        """Handle session event from background thread."""
        self._process_session_event(msg.event)

    def on_submit_complete(self, msg: SubmitComplete) -> None:
        """Handle submit completion."""
        if not self._finalize_streaming():
            self._submitting = False
            return
        result = msg.result
        if hasattr(result, "notice") and result.notice:
            self._output(result.notice, title="提示")
        if hasattr(result, "checkpoint") and self._should_show_checkpoint(result.checkpoint):
            self._output(result.checkpoint, title="检查点")
        self._submitting = False
        self._input_enabled(True)
        self._drain_pending_submissions()

    def on_submit_cancelled(self, msg: SubmitCancelled) -> None:
        """Handle submit cancellation."""
        if not self._finalize_streaming():
            self._submitting = False
            return
        block = TranscriptBlock("status", "回合已取消", "已按下 Ctrl+C", "cancelled")
        self.state.add_block(block)
        self._append_block(block)
        self._submitting = False
        self._input_enabled(True)
        self._drain_pending_submissions()

    def on_submit_error(self, msg: SubmitError) -> None:
        """Handle submit error."""
        if not self._finalize_streaming():
            self._submitting = False
            return
        self._output(f"错误：{msg.error}", title="错误")
        self._submitting = False
        if self.state.snapshot.status in {"running command", "loading history"}:
            self.state.snapshot.status = "idle"
            self._refresh_bars()
        self._input_enabled(True)
        self._drain_pending_submissions()

    def on_output_event(self, msg: OutputEvent) -> None:
        """Handle background thread output (compact, permission, etc.)."""
        if msg.title == "__refresh_only__":
            self._refresh_bars()
            self._redraw_transcript()
            return
        self._output(msg.text, title=msg.title)
        self._refresh_bars()

    # ── Callbacks for InteractiveSession ────────────────────────────────────

    def _stream_delta(self, delta: str) -> None:
        """Called from background thread. Post message to UI thread."""
        self.post_message(StreamDelta(delta))

    def _event_listener(self, event) -> None:
        """Called from background thread. Post message to UI thread."""
        if self.state is None:
            self._pending_events.append(event)
            return
        self.post_message(SessionEvent(event))

    def _startup_progress(self, stage: str) -> None:
        """Forward startup stages from the initialization thread."""
        self.post_message(StartupProgress(stage))

    def _output(self, text: str, *, title: str = "output") -> None:
        """Write a status block to the transcript."""
        # Only local slash-command/status results are translated. Error text,
        # tool output, and model-generated content must remain verbatim.
        display_text = (
            _localize_tui_output(text)
            if title in {"output", "提示", "检查点"}
            else str(text or "")
        )
        block = TranscriptBlock("status", _localize_tui_label(title), display_text)
        self.state.add_block(block)
        self._append_block(block)

    def _schedule_stream_flush(self) -> None:
        if self._stream_flush_scheduled:
            return
        self._stream_flush_scheduled = True
        self.set_timer(_STREAM_FLUSH_DELAY_SECONDS, self._flush_streaming_pending)

    def _flush_streaming_pending(self, *, drain_all: bool = False) -> None:
        self._stream_flush_scheduled = False
        if not self._pending_stream_chars:
            return
        delta = self._take_pending_stream_delta(
            None if drain_all else _STREAM_FLUSH_MAX_CHARS
        )
        if not delta:
            return
        try:
            transcript = self.query_one("#transcript", TranscriptView)
        except NoMatches:
            return
        if not self._stream_header_printed:
            transcript.begin_streaming()
            self._stream_header_printed = True
        self._streaming_current_response = True
        transcript.append_streaming(delta)
        self._redraw_transcript()
        if self._pending_stream_chars:
            self._schedule_stream_flush()

    def _take_pending_stream_delta(self, max_chars: int | None) -> str:
        if not self._pending_stream_chars:
            return ""
        if max_chars is None or self._pending_stream_chars <= max_chars:
            text = "".join(self._pending_stream_chunks)
            self._clear_pending_stream_deltas()
            return text

        remaining = max_chars
        parts: list[str] = []
        while remaining > 0 and self._pending_stream_chunks:
            chunk = self._pending_stream_chunks[0]
            if len(chunk) <= remaining:
                parts.append(self._pending_stream_chunks.popleft())
                self._pending_stream_chars -= len(chunk)
                remaining -= len(chunk)
                continue
            parts.append(chunk[:remaining])
            self._pending_stream_chunks[0] = chunk[remaining:]
            self._pending_stream_chars -= remaining
            remaining = 0
        return "".join(parts)

    def _clear_pending_stream_deltas(self) -> None:
        self._pending_stream_chunks.clear()
        self._pending_stream_chars = 0
        self._stream_flush_scheduled = False

    # ── Actions ─────────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        """Cancel the active turn."""
        if self._submitting:
            self._cancellation_token.cancel()
            try:
                self.session.interrupt_current_shell()
            except Exception:
                log.debug("Failed to interrupt shell during cancel", exc_info=True)

    def action_observe(self) -> None:
        """Open the temporary observability dashboard."""
        from .screens import ObservabilityScreen

        if hasattr(self, "session"):
            self.push_screen(ObservabilityScreen(self.session))

    def action_toggle_permission(self) -> None:
        """Toggle runtime permission mode (dispatched to worker thread)."""
        self._run_toggle_permission()

    @work(thread=True, exclusive=True)
    def _run_toggle_permission(self) -> None:
        """Worker thread: toggle permission and post result to UI thread."""
        try:
            self.session.toggle_permission_mode()
            self.state.snapshot.permission_mode = self.session.permission_mode
            self.post_message(OutputEvent("", title="__refresh_only__"))
        except Exception as exc:
            self.post_message(OutputEvent(f"错误：{exc}", title="权限模式错误"))

    def action_panel_key(self, key: str) -> None:
        """High-priority key binding for active approval/question panels."""
        self.route_active_panel_key(key)

    # ── Approval/Question panel management ──────────────────────────────────

    def show_approval_panel(
        self,
        request,
        event: threading.Event,
        result_holder: list,
    ) -> None:
        """Show approval panel replacing input area."""
        self._approval_event = event
        self._approval_result_holder = result_holder
        self._active_panel = "approval"
        self._input_enabled(False)

        from .screens import ApprovalPanel
        panel = ApprovalPanel(request, id="approval-panel")
        input_area = self.query_one("#input-area", InputArea)
        input_area.display = False
        # Mount after input-area
        self.mount(panel, after=input_area)
        self.call_after_refresh(panel.focus)

    def on_approval_result(self, msg: ApprovalResult) -> None:
        """Handle ApprovalResult message from ApprovalPanel."""
        approved = msg.approved
        try:
            self.query_one("#approval-panel").remove()
        except NoMatches:
            log.debug("Approval panel already removed")
        except Exception:
            log.warning("Error removing approval panel", exc_info=True)
        self._input_enabled(True)
        self._active_panel = None
        self._approval_result_holder[0] = approved
        if self._approval_event:
            self._approval_event.set()

    def show_question_panel(
        self,
        request,
        event: threading.Event,
        result_holder: list,
    ) -> None:
        """Show question panel replacing input area."""
        self._question_event = event
        self._question_result_holder = result_holder
        self._active_panel = "question"
        self._input_enabled(False)

        from .screens import QuestionPanel
        panel = QuestionPanel(request, id="question-panel")
        input_area = self.query_one("#input-area", InputArea)
        input_area.display = False
        self.mount(panel, after=input_area)
        self.call_after_refresh(panel.focus)

    def on_question_result(self, msg: QuestionResult) -> None:
        """Handle QuestionResult message from QuestionPanel."""
        payload = msg.payload
        try:
            self.query_one("#question-panel").remove()
        except NoMatches:
            log.debug("Question panel already removed")
        except Exception:
            log.warning("Error removing question panel", exc_info=True)
        self._input_enabled(True)
        self._active_panel = None
        self._question_result_holder[0] = payload
        if self._question_event:
            self._question_event.set()

    def _input_enabled(self, enabled: bool) -> None:
        """Enable or disable the input area."""
        try:
            input_area = self.query_one("#input-area", InputArea)
            input_area.display = enabled
            if enabled:
                input_area.focus_input()
        except NoMatches:
            log.debug("Input area not found in _input_enabled(%s)", enabled)
        except Exception:
            log.warning("Error in _input_enabled(%s)", enabled, exc_info=True)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _process_session_event(self, event) -> None:
        """Apply one session event to state and transcript.

        Handles both pre-mount buffered events and live events.
        """
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_type = data.get("type")
        if event_type == "turn_started":
            self._clear_pending_stream_deltas()
            self._streaming_current_response = False
            self._stream_header_printed = False

        if (
            self._streaming_current_response
            and self._pending_stream_chars
            and event_type not in {"thought_started", "thought_finished"}
        ):
            self._flush_streaming_pending(drain_all=True)

        if (
            self._streaming_current_response
            and event_type not in {"assistant_message", "thought_started", "thought_finished"}
        ):
            try:
                transcript = self.query_one("#transcript", TranscriptView)
            except NoMatches:
                transcript = None
            if transcript is not None:
                active_block = transcript.flush_streaming()
                self._add_block_if_new(active_block)
                self._streaming_current_response = False
                self._stream_header_printed = False
                self._redraw_transcript()

        block = self.state.apply_event(event)
        render_block = block

        # If we streamed the response, flush the buffered text as a complete block
        streamed_assistant_message = False
        if event_type == "assistant_message" and self._streaming_current_response:
            try:
                transcript = self.query_one("#transcript", TranscriptView)
            except NoMatches:
                transcript = None
            if transcript is not None:
                transcript.flush_streaming()
            render_block = None
            streamed_assistant_message = True
            self._streaming_current_response = False
            self._stream_header_printed = False

        added = self._add_block_if_new(block)
        if streamed_assistant_message:
            self._redraw_transcript()
        elif render_block is not None and added:
            self._append_block(render_block)

        self._refresh_bars()

    def _append_block(self, block: TranscriptBlock) -> None:
        try:
            transcript = self.query_one("#transcript", TranscriptView)
        except NoMatches:
            return
        transcript.append_block(block)

    def _finalize_streaming(self) -> bool:
        """Flush any pending streaming output. Returns False if UI is gone."""
        try:
            self._flush_streaming_pending(drain_all=True)
            transcript = self.query_one("#transcript", TranscriptView)
            block = transcript.flush_streaming()
            self._streaming_current_response = False
            self._stream_header_printed = False
            if self._add_block_if_new(block):
                self._redraw_transcript()
        except NoMatches:
            return False
        return True

    def _advance_spinner(self) -> None:
        try:
            self.query_one("#status-bar", StatusBar).advance_spinner()
        except NoMatches:
            pass

    def _refresh_bars(self) -> None:
        """Update the single terminal-native status line."""
        if self.state is None:
            return
        try:
            self._request_context_snapshot()
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.update_from_snapshot(self.state.snapshot)
        except NoMatches:
            log.debug("Status bar not found in _refresh_bars")
        except Exception:
            log.warning("Error refreshing bars", exc_info=True)

    def _request_context_snapshot(self) -> None:
        """Schedule a throttled context count without blocking the UI thread."""
        if (
            self.session is None
            or not getattr(self.session, "is_bound", True)
            or self.session.conversation is None
        ):
            self.state.snapshot.context_tokens = 0
            self.state.snapshot.context_window_tokens = config.CONTEXT_WINDOW_TOKENS
            return
        now = time.monotonic()
        if self._context_refresh_in_flight or now - self._last_context_refresh < _CONTEXT_SNAPSHOT_MIN_INTERVAL_SECONDS:
            return
        self._context_refresh_in_flight = True
        self._refresh_context_snapshot_worker(self.session)

    @work(thread=True, group="context-refresh", exclusive=True, exit_on_error=False)
    def _refresh_context_snapshot_worker(self, session) -> None:
        """Count request tokens off the UI thread; tool schemas can be costly."""
        from ..agent import context

        try:
            tool_schemas = getattr(getattr(session, "agent", None), "tool_schemas", None)
            context_tokens = context.count_request_tokens(
                list(session.conversation.messages),
                tool_schemas=tool_schemas,
            )
        except Exception:
            log.debug("Context token refresh failed", exc_info=True)
            context_tokens = 0
        self.post_message(
            ContextSnapshotReady(
                session,
                context_tokens,
                config.CONTEXT_WINDOW_TOKENS,
            )
        )

    def _redraw_transcript(self) -> None:
        try:
            transcript = self.query_one("#transcript", TranscriptView)
            transcript.clear()
            transcript.show_welcome(self.state.snapshot)
            for block in self.state.blocks:
                transcript.append_block(block)
            streaming_block = transcript.streaming_block()
            if streaming_block is not None:
                transcript.append_block(streaming_block)
        except NoMatches:
            log.debug("Transcript not found while redrawing")
        except Exception:
            log.warning("Error redrawing transcript", exc_info=True)

    def _last_block_matches(self, block: TranscriptBlock) -> bool:
        if self.state is None or not self.state.blocks:
            return False
        last = self.state.blocks[-1]
        return last.kind == block.kind and last.body == block.body

    def _add_block_if_new(self, block: TranscriptBlock | None) -> bool:
        if block is None:
            return False
        if self._last_block_matches(block):
            last = self.state.blocks[-1]
            if last.turn is None and block.turn is not None:
                last.turn = block.turn
            return False
        self.state.add_block(block)
        return True

    @staticmethod
    def _should_show_checkpoint(checkpoint: str) -> bool:
        text = str(checkpoint or "").strip()
        return text.startswith("checkpoint created:")

    def exit(self, *args, **kwargs) -> None:
        """Clean shutdown."""
        self._exiting = True
        if self._approval_event is not None and not self._approval_event.is_set():
            if self._approval_result_holder:
                self._approval_result_holder[0] = False
            self._approval_event.set()
        if self._question_event is not None and not self._question_event.is_set():
            if self._question_result_holder:
                self._question_result_holder[0] = None
            self._question_event.set()
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                log.debug("Error closing session during exit", exc_info=True)
        super().exit(*args, **kwargs)


def _preview_submission(text: str, limit: int = 80) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def _localize_tui_label(label: str) -> str:
    return {
        "output": "输出",
        "notice": "提示",
        "checkpoint": "检查点",
        "error": "错误",
        "permission mode error": "权限模式错误",
    }.get(str(label), str(label))


def _localize_tui_output(text: str) -> str:
    """Translate fixed runtime status phrases without touching model content."""
    replacements = {
        "profile switched: ": "配置已切换：",
        "profile selected: ": "已选择配置：",
        "profile already active: ": "配置已处于当前状态：",
        "permission mode switched: ": "权限模式已切换：",
        "permission mode already active: ": "权限模式已处于当前状态：",
        "checkpoint created: ": "检查点已创建：",
        "MCP reloaded": "MCP 已重新加载",
        "No compacted summary available yet.": "目前还没有可用的压缩摘要。",
        "Latest compacted summary:": "最近的压缩摘要：",
    }
    localized = str(text or "")
    for source, target in replacements.items():
        localized = localized.replace(source, target)
    if localized.startswith(("Observability dashboard", "Project observability")):
        from .screens import _localize_observability_text
        localized = _localize_observability_text(localized)
    return localized
