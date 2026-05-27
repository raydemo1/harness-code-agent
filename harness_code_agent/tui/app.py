from __future__ import annotations

import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from .. import config
from ..agent.cancellation import CancellationToken
from ..core.interactive import InteractiveSession
from ..core.mentions import MentionResolutionError
from .approval import TuiApprovalProvider
from .commands import default_command_registry
from .completion import HcaCompleter
from .question import TuiQuestionProvider
from .render import (
    context_bar_fragments,
    welcome_fragments,
)
from .state import SessionStatusSnapshot, TuiState


class TuiApp:
    def __init__(
        self,
        *,
        cwd: str | Path,
        profile_name: str,
        resume_session_id: str | None = None,
        first_task: str = "",
    ):
        self.cwd = Path(cwd).resolve()
        self.profile_name = profile_name
        self.resume_session_id = resume_session_id
        self.first_task = first_task
        self.registry = default_command_registry()
        self.state: TuiState | None = None
        self._pending_events = []
        self._streaming_current_response = False
        self._stream_header_printed = False
        self._submitting = False
        self._cancellation_token = CancellationToken()

        # Async infrastructure
        self._event_queue: queue.Queue = queue.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hca-tui")

        self._question_provider = TuiQuestionProvider()

        self.session = InteractiveSession(
            cwd=self.cwd,
            profile_name=profile_name,
            resume_session_id=resume_session_id,
            stream_sink=self._stream_delta,
            event_listener=self._event_listener,
            approval_provider=TuiApprovalProvider(project_root=self.cwd),
            question_provider=self._question_provider,
            output_sink=self._output,
        )
        self.state = TuiState(
            snapshot=SessionStatusSnapshot(
                profile=self.session.profile.name(),
                model=config.MODEL,
                provider=config.PROVIDER,
                permission_mode=self.session.permission_mode,
                session_id=self.session.session.id,
                cwd=self.session.cwd,
            )
        )
        for event in self._pending_events:
            self._handle_event(event)
        self._pending_events.clear()

        # Transcript control - dynamic content from state.blocks
        self._transcript_control = FormattedTextControl(
            text=self._get_transcript_text,
        )

        # Context bar control
        self._context_bar_control = FormattedTextControl(
            text=self._get_context_bar_text,
        )

        # Input area
        self._input_area = TextArea(
            multiline=True,
            wrap_lines=False,
            prompt="▶ ",
            completer=HcaCompleter(registry=self.registry, session=self.session),
            history=self._input_history(),
            accept_handler=self._on_input_accept,
        )

        # Key bindings
        kb = KeyBindings()

        @kb.add("c-c")
        def _(event):
            if self._submitting:
                self._cancel_current_turn()
            # Don't exit the application

        @kb.add("c-t")
        def _(event):
            if self.state:
                self.state.toggle_thought_details()
                self._refresh_display()

        @kb.add("escape", "enter", eager=True)
        def _(event):
            event.current_buffer.insert_text("\n")

        # Build layout
        from prompt_toolkit.layout.dimension import LayoutDimension as D

        transcript_window = Window(
            self._transcript_control,
            dont_extend_height=False,
            always_hide_cursor=True,
        )

        context_bar_window = Window(
            self._context_bar_control,
            height=D.exact(1),
            dont_extend_height=True,
            style="bg:#2d2d3d",
        )

        self.app = Application(
            layout=Layout(
                HSplit([
                    transcript_window,
                    Window(height=D.exact(1), char="─", style="#333333"),
                    context_bar_window,
                    Window(height=D.exact(1), char="─", style="#333333"),
                    self._input_area,
                ]),
            ),
            key_bindings=kb,
            full_screen=False,
            mouse_support=True,
        )
        self._question_provider.app = self.app

    def run(self) -> int:
        assert self.state is not None
        self.state.add_transcript_fragments(welcome_fragments(self.state.snapshot))
        if self.first_task:
            self._submit_async(self.first_task)
        try:
            self.app.run()
        except EOFError:
            pass
        finally:
            self._executor.shutdown(wait=False)
            self.session.close()
        return 0

    def _submit_async(self, text: str) -> None:
        """Dispatch submit to background thread. Non-blocking."""
        if self._submitting:
            return
        self._submitting = True
        self._streaming_current_response = False
        self._stream_header_printed = False
        self._cancellation_token = CancellationToken()
        self._executor.submit(self._submit_worker, text)

    def _submit_worker(self, text: str) -> None:
        """Runs in background thread."""
        from ..agent.cancellation import CancelledError
        try:
            result = self.session.submit(text, cancellation_token=self._cancellation_token)
            self._event_queue.put(("result", result))
        except CancelledError:
            self._event_queue.put(("cancelled", None))
        except MentionResolutionError as exc:
            self._event_queue.put(("error", str(exc)))
        except Exception as exc:
            self._event_queue.put(("error", str(exc)))
        finally:
            self._event_queue.put(("done", None))
            self._refresh_display()

    def _submit(self, text: str) -> None:
        """Synchronous submit (for initial task before app.run)."""
        self._submitting = True
        self._streaming_current_response = False
        self._stream_header_printed = False
        try:
            result = self.session.submit(text)
        except MentionResolutionError as exc:
            self._output(f"Error: {exc}", title="mention error")
            self._submitting = False
            return
        if result.notice:
            self._output(result.notice, title="notice")
        if result.checkpoint:
            self._output(result.checkpoint, title="checkpoint")
        self._submitting = False

    def _on_input_accept(self, buff: Buffer) -> None:
        """Called when user presses Enter in the input area."""
        text = buff.text.strip()
        if not text:
            return
        if text.startswith("/"):
            if not self.session.handle_slash_command(text):
                self.app.exit()
            buff.reset()
            return
        buff.reset()
        self._submit_async(text)

    def _event_listener(self, event) -> None:
        """Called from background thread. Queue event for UI thread."""
        if self.state is None:
            self._pending_events.append(event)
            return
        self._event_queue.put(("event", event))
        self._refresh_display()

    def _handle_event(self, event) -> None:
        """Process an event on the UI thread."""
        assert self.state is not None
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_type = data.get("type")
        if event_type == "turn_started":
            self._streaming_current_response = False
            self._stream_header_printed = False
        block = self.state.apply_event(event)
        if event_type == "assistant_message" and self._streaming_current_response:
            block = None
        self.state.add_block(block)
        if block is not None:
            self.state.add_block_fragments(block)

    def _drain_event_queue(self) -> None:
        """Process all queued events on the UI thread. Called before paint."""
        while True:
            try:
                kind, data = self._event_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                self._handle_event(data)
            elif kind == "result":
                result = data
                if result.notice:
                    self._output(result.notice, title="notice")
                if result.checkpoint:
                    self._output(result.checkpoint, title="checkpoint")
            elif kind == "error":
                self._output(f"Error: {data}", title="error")
            elif kind == "done":
                self._submitting = False
            elif kind == "stream":
                self._handle_stream_delta(data)
            elif kind == "cancelled":
                from .state import TranscriptBlock
                block = TranscriptBlock("status", "turn cancelled", "Ctrl-C pressed", "cancelled")
                self.state.add_block(block)
                self.state.add_block_fragments(block)

    def _cancel_current_turn(self) -> None:
        """Cancel the active turn and best-effort interrupt any running shell."""
        self._cancellation_token.cancel()
        try:
            self.session.interrupt_current_shell()
        except Exception:
            pass

    def _stream_delta(self, delta: str) -> None:
        """Called from background thread. Queue streaming delta."""
        self._event_queue.put(("stream", delta))
        self._refresh_display()

    def _handle_stream_delta(self, delta: str) -> None:
        """Process streaming delta on UI thread."""
        if not self._stream_header_printed:
            self.state.add_block_fragments_simple("assistant", "assistant", "")
            self._stream_header_printed = True
        self._streaming_current_response = True
        self.state.append_streaming_text(delta)

    def _output(self, text: str, *, title: str = "output") -> None:
        from .state import TranscriptBlock
        block = TranscriptBlock("status", title, text)
        self.state.add_block(block)
        self.state.add_block_fragments(block)

    def _refresh_display(self) -> None:
        """Trigger UI redraw from any thread."""
        if self.app and self.app.is_running:
            self.app.invalidate()

    def _get_transcript_text(self):
        """Dynamic text callback for the transcript control."""
        # Drain any pending events before rendering
        self._drain_event_queue()
        if self.state is None:
            return FormattedText([("", "")])
        # Handle queued stream deltas
        return FormattedText(self.state.transcript_fragments)

    def _get_context_bar_text(self):
        """Dynamic text callback for the context bar."""
        if self.state is None:
            return FormattedText([("", "")])
        self._refresh_context_snapshot()
        return FormattedText(context_bar_fragments(self.state.snapshot))

    def _refresh_context_snapshot(self) -> None:
        from ..agent import context
        from ..agent.compaction import get_thresholds
        thresholds = get_thresholds()
        self.state.snapshot.context_tokens = context.count_tokens(self.session.conversation.messages)
        self.state.snapshot.context_window_tokens = config.CONTEXT_WINDOW_TOKENS
        self.state.snapshot.context_observe_threshold = thresholds.observe
        self.state.snapshot.context_prepare_threshold = thresholds.prepare
        self.state.snapshot.context_allow_threshold = thresholds.allow
        self.state.snapshot.context_force_threshold = thresholds.force

    def _input_history(self):
        from prompt_toolkit.history import InMemoryHistory
        return InMemoryHistory()
