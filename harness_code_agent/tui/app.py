"""Textual-based TUI for Harness Code Agent."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import TextArea

log = logging.getLogger("harness.tui")

from .. import config
from ..agent.cancellation import CancelledError, CancellationToken
from ..core.interactive import InteractiveSession
from ..core.mentions import MentionResolutionError
from .approval import TuiApprovalProvider
from .commands import default_command_registry
from .question import TuiQuestionProvider
from .screens import ApprovalResult, QuestionResult
from .state import SessionStatusSnapshot, TranscriptBlock, TuiState
from .widgets import (
    ContextBar,
    InputArea,
    PlanPanel,
    StatusBar,
    TranscriptView,
)


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
    """Submit was cancelled by user."""
    def __init__(self) -> None:
        super().__init__()


class SubmitError(Message):
    """Submit failed with an error."""
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class OutputEvent(Message):
    """Output a status block to the transcript from a worker thread."""
    def __init__(self, text: str, title: str = "output") -> None:
        super().__init__()
        self.text = text
        self.title = title


# ── TuiApp ──────────────────────────────────────────────────────────────────

class TuiApp(App):
    """Full-screen Textual TUI for Harness Code Agent."""

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel", show=False, priority=True),
        Binding("ctrl+t", "toggle_thought", "Toggle thought", show=False, priority=True),
        Binding("ctrl+d", "toggle_details", "Details", show=False, priority=True),
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
    }

    #main-area {
        height: 1fr;
    }

    #transcript {
        width: 1fr;
        height: 100%;
        border: solid #333333;
    }

    #plan-panel {
        width: 38;
        height: 100%;
        border: solid #333333;
        padding: 1;
    }

    #input-area {
        height: auto;
        max-height: 10;
        border: solid #333333;
    }

    #cmd-palette {
        display: none;
        height: auto;
        max-height: 10;
        background: $surface;
        border: solid #555555;
    }

    #input-text {
        height: 3;
    }

    #status-bar {
        height: 1;
        background: #2d2d3d;
    }

    #context-bar {
        height: 1;
        background: #2d2d3d;
    }

    /* Approval panel */
    .approval-panel {
        height: auto;
        border: solid #b16286;
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
        border: solid #3874cb;
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
        self.registry = default_command_registry()
        self.state: TuiState | None = None
        self._pending_events: list = []
        self._streaming_current_response = False
        self._stream_header_printed = False
        self._submitting = False
        self._cancellation_token = CancellationToken()

        # Approval/question state
        self._approval_event: threading.Event | None = None
        self._approval_result_holder: list = []
        self._question_event: threading.Event | None = None
        self._question_result_holder: list = []
        self._active_panel: str | None = None  # "approval" or "question"

    def run(self, *args, **kwargs) -> int:
        """Run the Textual app and preserve the hca CLI exit-code contract."""
        super().run(*args, **kwargs)
        return 0

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "panel_key":
            return self._active_panel is not None
        return True

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-area"):
            yield TranscriptView(id="transcript")
            yield PlanPanel(id="plan-panel")
        yield InputArea(registry=self.registry, id="input-area")
        yield StatusBar(id="status-bar")
        yield ContextBar(id="context-bar")

    def on_mount(self) -> None:
        """Create session and initialize state."""
        # Build approval/question providers with reference to self
        approval_provider = TuiApprovalProvider(project_root=self.cwd, app_tui=self)
        question_provider = TuiQuestionProvider(app_tui=self)

        self.session = InteractiveSession(
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
        )
        display_profile = getattr(self.session, "display_profile", self.session.profile.name())
        session_id = getattr(
            self.session,
            "session_id",
            getattr(getattr(self.session, "session", None), "id", None),
        )
        is_bound = getattr(self.session, "is_bound", True)
        self.state = TuiState(
            snapshot=SessionStatusSnapshot(
                profile=display_profile,
                model=config.MODEL,
                provider=config.PROVIDER,
                permission_mode=self.session.permission_mode,
                session_id=session_id,
                cwd=self.session.cwd,
                status="idle" if is_bound else "pending",
            )
        )
        # Process any events that arrived before state was ready
        for event in self._pending_events:
            self._handle_event(event)
        self._pending_events.clear()

        # Wire up input area
        input_area = self.query_one("#input-area", InputArea)
        input_area.set_session(self.session)

        # Show welcome message
        transcript = self.query_one("#transcript", TranscriptView)
        transcript.show_welcome(self.state.snapshot)

        # Update status/context bars
        self._refresh_bars()

        # Focus input
        input_area.focus_input()

        # Submit first task if provided
        if self.first_task:
            self._submit_async(self.first_task)

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

    def _handle_event(self, event) -> None:
        """Process a raw session event (not a Textual Message). Used in on_mount."""
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_type = data.get("type")
        if event_type == "turn_started":
            self._streaming_current_response = False
            self._stream_header_printed = False

        block = self.state.apply_event(event)
        render_block = None if event_type == "assistant_message" and self._streaming_current_response else block

        added = self._add_block_if_new(block)
        if render_block is not None and added:
            transcript = self.query_one("#transcript", TranscriptView)
            transcript.append_block(render_block)

        self._refresh_bars()

    # ── Async submit ────────────────────────────────────────────────────────

    def _submit_async(self, text: str) -> bool:
        """Dispatch submit to background worker. Non-blocking."""
        if self._submitting:
            return False
        self._submitting = True
        self._input_enabled(False)
        self._streaming_current_response = False
        self._stream_header_printed = False
        self._cancellation_token = CancellationToken()
        self._submit_worker(text)
        return True

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _submit_worker(self, text: str) -> None:
        """Run session.submit() in a background thread."""
        try:
            result = self.session.submit(text, cancellation_token=self._cancellation_token)
            self.post_message(SubmitComplete(result))
        except CancelledError:
            self.post_message(SubmitCancelled())
        except MentionResolutionError as exc:
            self.post_message(SubmitError(str(exc)))
        except Exception as exc:
            self.post_message(SubmitError(str(exc)))

    # ── Message handlers (UI thread) ────────────────────────────────────────

    def on_stream_delta(self, msg: StreamDelta) -> None:
        """Handle streaming delta from background thread.

        Text is buffered in TranscriptView and written as a complete
        Markdown Panel when streaming finishes, avoiding fragmentation.
        """
        transcript = self.query_one("#transcript", TranscriptView)
        if not self._stream_header_printed:
            transcript.begin_streaming()
            self._stream_header_printed = True
        self._streaming_current_response = True
        self.state.append_streaming_text(msg.delta)
        transcript.append_streaming(msg.delta)
        self._redraw_transcript()

    def on_session_event(self, msg: SessionEvent) -> None:
        """Handle session event from background thread."""
        data = msg.event.to_dict() if hasattr(msg.event, "to_dict") else dict(msg.event)
        event_type = data.get("type")
        if event_type == "turn_started":
            self._streaming_current_response = False
            self._stream_header_printed = False

        if (
            self._streaming_current_response
            and event_type not in {"assistant_message", "thought_started", "thought_finished"}
        ):
            transcript = self.query_one("#transcript", TranscriptView)
            active_block = transcript.flush_streaming()
            self._add_block_if_new(active_block)
            self._streaming_current_response = False
            self._stream_header_printed = False
            self._redraw_transcript()

        block = self.state.apply_event(msg.event)
        render_block = block

        # If we streamed the response, flush the buffered text as a complete block
        streamed_assistant_message = False
        if event_type == "assistant_message" and self._streaming_current_response:
            transcript = self.query_one("#transcript", TranscriptView)
            transcript.flush_streaming()
            render_block = None
            streamed_assistant_message = True
            self._streaming_current_response = False
            self._stream_header_printed = False

        added = self._add_block_if_new(block)
        if streamed_assistant_message:
            self._redraw_transcript()
        elif render_block is not None and added:
            transcript = self.query_one("#transcript", TranscriptView)
            transcript.append_block(render_block)

        self._refresh_bars()

    def on_submit_complete(self, msg: SubmitComplete) -> None:
        """Handle submit completion."""
        # Flush any remaining streaming buffer
        try:
            transcript = self.query_one("#transcript", TranscriptView)
            block = transcript.flush_streaming()
            self._streaming_current_response = False
            self._stream_header_printed = False
            if self._add_block_if_new(block):
                self._redraw_transcript()
        except NoMatches:
            self._submitting = False
            return
        result = msg.result
        if hasattr(result, "notice") and result.notice:
            self._output(result.notice, title="notice")
        if hasattr(result, "checkpoint") and self._should_show_checkpoint(result.checkpoint):
            self._output(result.checkpoint, title="checkpoint")
        self._submitting = False
        self._input_enabled(True)

    def on_submit_cancelled(self, msg: SubmitCancelled) -> None:
        """Handle submit cancellation."""
        try:
            transcript = self.query_one("#transcript", TranscriptView)
            block = transcript.flush_streaming()
            self._streaming_current_response = False
            self._stream_header_printed = False
            self._add_block_if_new(block)
        except NoMatches:
            self._submitting = False
            return
        block = TranscriptBlock("status", "turn cancelled", "Ctrl-C pressed", "cancelled")
        self.state.add_block(block)
        transcript.append_block(block)
        self._submitting = False
        self._input_enabled(True)

    def on_submit_error(self, msg: SubmitError) -> None:
        """Handle submit error."""
        try:
            transcript = self.query_one("#transcript", TranscriptView)
            block = transcript.flush_streaming()
            self._streaming_current_response = False
            self._stream_header_printed = False
            self._add_block_if_new(block)
        except NoMatches:
            self._submitting = False
            return
        self._output(f"Error: {msg.error}", title="error")
        self._submitting = False
        self._input_enabled(True)

    def on_output_event(self, msg: OutputEvent) -> None:
        """Handle background thread output (compact, permission, etc.)."""
        if msg.title == "__refresh_only__":
            self._refresh_bars()
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

    def _output(self, text: str, *, title: str = "output") -> None:
        """Write a status block to the transcript."""
        block = TranscriptBlock("status", title, text)
        self.state.add_block(block)
        transcript = self.query_one("#transcript", TranscriptView)
        transcript.append_block(block)

    # ── Actions ─────────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        """Cancel the active turn."""
        if self._submitting:
            self._cancellation_token.cancel()
            try:
                self.session.interrupt_current_shell()
            except Exception:
                log.debug("Failed to interrupt shell during cancel", exc_info=True)

    def action_toggle_thought(self) -> None:
        """Toggle thought detail visibility."""
        if self.state:
            self.state.toggle_thought_details()

    def action_toggle_details(self) -> None:
        """Toggle the most recent folded turn details."""
        if self.state and self.state.toggle_latest_turn_details():
            self._redraw_transcript()

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
            self.post_message(OutputEvent(f"Error: {exc}", title="permission mode error"))

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

    def _refresh_bars(self) -> None:
        """Update StatusBar and ContextBar from current state."""
        if self.state is None:
            return
        try:
            self._refresh_context_snapshot()
            self.query_one("#plan-panel", PlanPanel).update_steps(self.state.plan_steps)
            self.query_one("#status-bar", StatusBar).update_from_snapshot(self.state.snapshot)
            self.query_one("#context-bar", ContextBar).update_from_snapshot(self.state.snapshot)
        except NoMatches:
            log.debug("Status/Context bar not found in _refresh_bars")
        except Exception:
            log.warning("Error refreshing bars", exc_info=True)

    def _refresh_context_snapshot(self) -> None:
        """Refresh context token counts."""
        if not getattr(self.session, "is_bound", True) or self.session.conversation is None:
            self.state.snapshot.context_tokens = 0
            self.state.snapshot.context_window_tokens = config.CONTEXT_WINDOW_TOKENS
            return
        from ..agent import context
        from ..agent.compaction import get_thresholds
        thresholds = get_thresholds()
        tool_schemas = getattr(getattr(self.session, "agent", None), "tool_schemas", None)
        self.state.snapshot.context_tokens = context.count_request_tokens(
            self.session.conversation.messages,
            tool_schemas=tool_schemas,
        )
        self.state.snapshot.context_window_tokens = config.CONTEXT_WINDOW_TOKENS
        self.state.snapshot.context_compact_threshold = thresholds.compact

    def _redraw_transcript(self) -> None:
        try:
            transcript = self.query_one("#transcript", TranscriptView)
            transcript.clear()
            transcript.show_welcome(self.state.snapshot)
            for block in self.state.visible_blocks():
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
        if self._approval_event is not None and not self._approval_event.is_set():
            if self._approval_result_holder:
                self._approval_result_holder[0] = False
            self._approval_event.set()
        if self._question_event is not None and not self._question_event.is_set():
            if self._question_result_holder:
                self._question_result_holder[0] = None
            self._question_event.set()
        try:
            self.session.close()
        except Exception:
            log.debug("Error closing session during exit", exc_info=True)
        super().exit(*args, **kwargs)
