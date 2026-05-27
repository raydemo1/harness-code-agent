from __future__ import annotations

from textwrap import fill
from typing import Callable, Any

from prompt_toolkit import print_formatted_text
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from ..runtime.questions import (
    QuestionRequest,
    QuestionResult,
    question_result_from_option,
)


_QUESTION_STYLE = Style.from_dict(
    {
        "question.choice": "ansigray",
        "question.choice.selected": "reverse ansicyan bold",
        "question.help": "ansigray",
        "question.input": "ansicyan",
    }
)


class TuiQuestionProvider:
    def __init__(self, *, choice_bar_factory: Callable[..., "QuestionChoiceBar"] | None = None):
        self.choice_bar_factory = choice_bar_factory or QuestionChoiceBar

    def ask(self, request: QuestionRequest) -> QuestionResult:
        try:
            payload = self.choice_bar_factory(request).run()
        except (EOFError, KeyboardInterrupt):
            print_formatted_text(HTML("\n<ansired>Question cancelled/interrupted.</ansired>"))
            return QuestionResult(cancelled=True, reason="interrupted in TUI", metadata={"ui": "tui"})

        if isinstance(payload, QuestionResult):
            payload.metadata.setdefault("ui", "tui")
            return payload
        return _question_result_from_payload(request, payload)


class QuestionChoiceBar:
    def __init__(self, request: QuestionRequest):
        self.request = request
        self.selected_index = 0
        self.other_text = ""
        self._armed_number_key: str | None = None

    def run(self) -> dict[str, Any]:
        bindings = KeyBindings()

        @bindings.add("right")
        @bindings.add("down")
        @bindings.add("tab")
        def _(event):
            self._select((self.selected_index + 1) % len(self.request.options))

        @bindings.add("left")
        @bindings.add("up")
        @bindings.add("s-tab")
        def _(event):
            self._select((self.selected_index - 1) % len(self.request.options))

        @bindings.add("enter")
        def _(event):
            event.app.exit(result=self._result_payload())

        @bindings.add("backspace")
        @bindings.add("c-h")
        def _(event):
            if self._selected_is_other() and self.other_text:
                self.other_text = self.other_text[:-1]
                self._armed_number_key = None
                event.app.invalidate()

        @bindings.add("c-c")
        def _(event):
            event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("c-d")
        def _(event):
            event.app.exit(exception=EOFError())

        for number in range(1, min(9, len(self.request.options)) + 1):
            key = str(number)

            @bindings.add(key, eager=True)
            def _(event, key=key):
                payload = self.handle_number_key(key)
                if payload is not None:
                    event.app.exit(result=payload)
                    return
                event.app.invalidate()

        @bindings.add(Keys.Any)
        def _(event):
            key = event.key_sequence[0].key
            if not self._selected_is_other() or len(str(key)) != 1:
                return
            text = str(key)
            if text.isprintable():
                self.other_text += text
                self._armed_number_key = None
                event.app.invalidate()

        body = Window(
            FormattedTextControl(lambda: _format_question_body(self.request)),
            wrap_lines=True,
        )
        spacer = Window(height=1)
        choices = Window(
            FormattedTextControl(
                lambda: _format_question_choice_bar(
                    self.request.options,
                    selected_index=self.selected_index,
                    other_text=self.other_text,
                )
            ),
            height=3,
        )
        app: Application[dict[str, Any]] = Application(
            layout=Layout(HSplit([body, spacer, choices])),
            key_bindings=bindings,
            full_screen=False,
            erase_when_done=False,
            style=_QUESTION_STYLE,
        )
        return app.run() or self._result_payload()

    def handle_number_key(self, key: str) -> dict[str, Any] | None:
        if not key.isdigit():
            return None
        target = int(key) - 1
        if target < 0 or target >= len(self.request.options):
            return None
        if self.selected_index == target and self._armed_number_key == key:
            return self._result_payload()
        self.selected_index = target
        self._armed_number_key = key
        return None

    def _select(self, index: int) -> None:
        self.selected_index = index
        self._armed_number_key = None

    def _selected_is_other(self) -> bool:
        return self.request.options[self.selected_index].is_other

    def _result_payload(self) -> dict[str, Any]:
        result = question_result_from_option(
            self.request,
            self.selected_index,
            custom_text=self.other_text.strip(),
            metadata={"ui": "tui"},
        )
        return result.to_dict()


def _format_question_body(request: QuestionRequest) -> str:
    lines = ["", "Question", *_wrap_field("prompt", request.question)]
    for index, option in enumerate(request.options, start=1):
        if option.description:
            lines.extend(_wrap_field(f"{index}. {option.label}", option.description))
    return "\n".join(lines)


def _format_question_choice_bar(
    options,
    *,
    selected_index: int,
    other_text: str = "",
) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = [("class:question.help", "Use arrows/tab, Enter to choose  ")]
    for index, option in enumerate(options):
        style = "class:question.choice.selected" if index == selected_index else "class:question.choice"
        fragments.append((style, f" {index + 1} {option.label} "))
        fragments.append(("", " "))
    fragments.append(("class:question.help", " shortcuts: press a number twice to submit"))
    other = next((option for option in options if option.is_other), None)
    if other is not None:
        input_style = "class:question.input" if options[selected_index].is_other else "class:question.help"
        fragments.append(("", "\n"))
        fragments.append((input_style, f"{other.label}: {other_text}"))
    return fragments


def _wrap_field(label: str, value: object, *, width: int = 100) -> list[str]:
    prefix = f"{label}: "
    text = str(value)
    if not text:
        return [prefix.rstrip()]
    return fill(
        prefix + text,
        width=width,
        subsequent_indent=" " * len(prefix),
        break_long_words=False,
        break_on_hyphens=False,
    ).splitlines()


def _question_result_from_payload(request: QuestionRequest, payload: Any) -> QuestionResult:
    if not isinstance(payload, dict):
        return QuestionResult(cancelled=True, reason="invalid TUI question result", metadata={"ui": "tui"})
    selected_index = payload.get("selected_index")
    if not isinstance(selected_index, int) or selected_index < 0 or selected_index >= len(request.options):
        return QuestionResult(cancelled=True, reason="invalid TUI question option", metadata={"ui": "tui"})
    result = question_result_from_option(
        request,
        selected_index,
        custom_text=str(payload.get("custom_text") or ""),
        metadata=dict(payload.get("metadata") or {"ui": "tui"}),
        reason=str(payload.get("reason") or "selected by user"),
    )
    return result
