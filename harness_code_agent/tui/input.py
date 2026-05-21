from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from .completion import HcaCompleter
from .render import bottom_toolbar, prompt_message


class TuiComposer:
    def __init__(self, *, registry, session, state):
        self.state = state
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
            bottom_toolbar=lambda: bottom_toolbar(self.state.snapshot),
        )

    def prompt(self) -> str:
        return self.prompt_session.prompt(prompt_message(self.state.snapshot))
