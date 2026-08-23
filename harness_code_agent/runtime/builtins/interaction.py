"""User interaction tools."""
from __future__ import annotations

import json

from ..questions import (
    ConsoleQuestionProvider,
    QuestionRequest,
    normalize_question_options,
)
from ..tool_context import ToolContext
from ..tool_result import ToolResult


def ask_user(
    question: str,
    options: list | None,
    other_label: str = "其他",
    agent_name: str | None = None,
    tool_context: ToolContext | None = None,
) -> ToolResult:
    """Ask the user a multiple-choice question and return the selected answer as JSON."""
    question = (question or "").strip()
    if not question:
        return ToolResult(
            tool="ask_user",
            status="failed",
            output="[error] ask_user requires a non-empty question",
            error="ask_user requires a non-empty question",
            metadata={"status_source": "validation"},
        )

    normalized_options = normalize_question_options(options, other_label=(other_label or "其他"))
    if len(normalized_options) <= 1:
        return ToolResult(
            tool="ask_user",
            status="failed",
            output="[error] ask_user requires at least one non-Other option",
            error="at least one option required",
            metadata={"status_source": "validation"},
        )
    request = QuestionRequest(
        question=question,
        options=normalized_options,
        agent_name=agent_name,
        session_id=tool_context.session_id if tool_context is not None else None,
    )
    provider = tool_context.question_provider if tool_context is not None else ConsoleQuestionProvider()
    result = provider.ask(request)
    if result.cancelled:
        reason = result.reason or "question cancelled"
        return ToolResult(
            tool="ask_user",
            status="failed",
            output=f"[cancelled] {reason}",
            error=reason,
            metadata={"status_source": "user_question", **result.metadata},
        )

    return ToolResult(
        tool="ask_user",
        status="success",
        output=json.dumps(result.to_dict(), ensure_ascii=False),
        metadata={
            "status_source": "user_question",
            "selected_index": result.selected_index,
            "is_other": result.is_other,
        },
    )
