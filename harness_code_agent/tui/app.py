from __future__ import annotations

import sys
from pathlib import Path

from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit import print_formatted_text

from .. import config
from ..core.interactive import InteractiveSession
from ..core.mentions import MentionResolutionError
from .approval import TuiApprovalProvider
from .commands import default_command_registry
from .input import TuiComposer
from .render import print_block, print_output, print_welcome
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

        self.session = InteractiveSession(
            cwd=self.cwd,
            profile_name=profile_name,
            resume_session_id=resume_session_id,
            stream_sink=self._stream_delta,
            event_listener=self._event_listener,
            approval_provider=TuiApprovalProvider(),
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
        self.composer = TuiComposer(registry=self.registry, session=self.session, state=self.state)

    def run(self) -> int:
        assert self.state is not None
        print_welcome(self.state.snapshot)
        try:
            with patch_stdout(raw=True):
                if self.first_task:
                    self._submit(self.first_task)
                while True:
                    try:
                        line = self.composer.prompt()
                    except EOFError:
                        print()
                        break
                    line = line.strip()
                    if not line:
                        if self.state.snapshot.pending_plan:
                            self._submit("continue")
                        continue
                    if line.startswith("/"):
                        if not self.session.handle_slash_command(line):
                            break
                        continue
                    self._submit(line)
        except KeyboardInterrupt:
            print_formatted_text(HTML("\n<ansiyellow>Interrupted.</ansiyellow>"))
            return 130
        finally:
            self.session.close()
        return 0

    def _submit(self, text: str) -> None:
        self._streaming_current_response = False
        self._stream_header_printed = False
        try:
            result = self.session.submit(text)
        except MentionResolutionError as exc:
            self._output(f"Error: {exc}", title="mention error")
            return
        if self._streaming_current_response:
            sys.stdout.write("\n")
            sys.stdout.flush()
        if result.notice:
            self._output(result.notice, title="notice")
        if result.checkpoint:
            self._output(result.checkpoint, title="checkpoint")

    def _event_listener(self, event) -> None:
        if self.state is None:
            self._pending_events.append(event)
            return
        self._handle_event(event)

    def _handle_event(self, event) -> None:
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
            print_block(block)

    def _stream_delta(self, delta: str) -> None:
        if not self._stream_header_printed:
            print_formatted_text(HTML("\n<ansigreen>▸ assistant</ansigreen>"))
            self._stream_header_printed = True
        self._streaming_current_response = True
        sys.stdout.write(delta)
        sys.stdout.flush()

    def _output(self, text: str, *, title: str = "output") -> None:
        print_output(text, title=title)
