"""Question provider for the Textual TUI."""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from ..runtime.questions import (
    QuestionRequest,
    QuestionResult,
    question_result_from_option,
)

if TYPE_CHECKING:
    from .app import TuiApp


class TuiQuestionProvider:
    def __init__(
        self,
        *,
        app_tui: "TuiApp | None" = None,
    ):
        self.app_tui = app_tui

    def ask(self, request: QuestionRequest) -> QuestionResult:
        if self.app_tui is None:
            return QuestionResult(cancelled=True, reason="no TUI app available", metadata={"ui": "tui"})

        # Bridge: worker thread → UI thread via call_from_thread + Event
        event = threading.Event()
        result_holder: list = [None]

        def _show():
            self.app_tui.show_question_panel(request, event, result_holder)

        try:
            self.app_tui.call_from_thread(_show)
        except Exception:
            return QuestionResult(cancelled=True, reason="failed to show question panel", metadata={"ui": "tui"})

        # Block until user answers
        event.wait()

        payload = result_holder[0] if result_holder else None

        if payload is None:
            return QuestionResult(cancelled=True, reason="cancelled in TUI", metadata={"ui": "tui"})

        if isinstance(payload, QuestionResult):
            payload.metadata.setdefault("ui", "tui")
            return payload

        return _question_result_from_payload(request, payload)


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
