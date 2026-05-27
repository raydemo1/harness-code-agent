from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from .. import config
from ..agent import context
from ..agent.compaction import get_thresholds
from .completion import HcaCompleter
from .render import bottom_toolbar, prompt_message


class TuiComposer:
    def __init__(self, *, registry, session, state):
        self.state = state
        self.session = session
        bindings = KeyBindings()

        @bindings.add("enter")
        def _(event):
            buffer = event.current_buffer
            if buffer.complete_state and buffer.complete_state.current_completion:
                buffer.apply_completion(buffer.complete_state.current_completion)
                return
            buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")

        self.prompt_session = PromptSession(
            completer=HcaCompleter(registry=registry, session=session),
            history=InMemoryHistory(),
            key_bindings=bindings,
            complete_while_typing=True,
            multiline=True,
            mouse_support=True,
            bottom_toolbar=self._bottom_toolbar,
        )

    def prompt(self) -> str:
        self._refresh_context_snapshot()
        return self.prompt_session.prompt(prompt_message(self.state.snapshot))

    def _bottom_toolbar(self):
        self._refresh_context_snapshot()
        return bottom_toolbar(self.state.snapshot, on_context_click=self._manual_compact_from_toolbar)

    def _refresh_context_snapshot(self) -> None:
        thresholds = get_thresholds()
        self.state.snapshot.context_tokens = context.count_tokens(self.session.conversation.messages)
        self.state.snapshot.context_window_tokens = config.CONTEXT_WINDOW_TOKENS
        self.state.snapshot.context_observe_threshold = thresholds.observe
        self.state.snapshot.context_prepare_threshold = thresholds.prepare
        self.state.snapshot.context_allow_threshold = thresholds.allow
        self.state.snapshot.context_force_threshold = thresholds.force

    def _manual_compact_from_toolbar(self) -> None:
        def compact_and_report() -> None:
            result = self.session.manual_compact_context()
            self._refresh_context_snapshot()
            output = getattr(self.session, "output_sink", print)
            output(result)

        run_in_terminal(compact_and_report)
