"""Asynchronous fast-model audit for Terminal acceptance checks."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from ..tool_result import ToolResult
from .base import MAIN_AGENT_NAMES, AgentMiddleware


@dataclass(frozen=True)
class ReviewOutcome:
    raw: str
    usage: dict | None
    provider: str
    model: str


Reviewer = Callable[..., str | ReviewOutcome]


ACCEPTANCE_REVIEW_SYSTEM_PROMPT = (
    "You are a fast plan auditor for a terminal coding agent. Review the original task, "
    "the agent's start plan, and its acceptance checks. Return concise plain text for the "
    "main agent, not JSON. Do not rewrite the whole plan and do not claim you ran commands. "
    "First audit whether the start plan follows the Spec/Risks/Validation/Implement rhythm: "
    "Spec should capture exact deliverables and non-negotiable external contracts; Risks "
    "should name likely hidden-verifier checks; Validation should propose failing commands or "
    "small assertion scripts before implementation. Then check whether the plan preserves the "
    "task's literal external contract instead of replacing it with a local substitute. Look for "
    "broad quality gaps, not task-specific tricks: sample-only checks, visible-helper overfit, "
    "non-failing checks, local-only substitutes, literal contract drift in paths/names/formats/"
    "protocols/ports/outputs, and important constraints without command-backed evidence. Prefer "
    "simple commands or small assertion scripts that use files/tools actually named or discoverable "
    "in the task environment; do not invent hidden oracle commands or assume private ground-truth "
    "files exist. If the plan is good enough, say that briefly. Otherwise, point to the highest-risk "
    "omissions and suggest whether the next action should be replan, inspect, implement, or verify. "
    "Keep the audit under 12 lines."
)


class AcceptanceReviewMiddleware(AgentMiddleware):
    """Start a plain-text plan audit and surface it without blocking execution."""

    EDIT_TOOLS: ClassVar[set] = {"write_file", "apply_patch"}

    def __init__(
        self,
        *,
        reviewer: Reviewer | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
    ):
        self._reviewer = reviewer or _call_fast_reviewer
        self._timeout_seconds = max(0.01, float(timeout_seconds))
        self._max_attempts = max(1, int(max_attempts))

    def before_tool(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or runtime_state is None:
            return None
        self._flush_events(runtime_state)
        # The audit is advisory. Waiting here used to turn a fast background
        # review into a hard gate on the first edit, creating an avoidable LLM
        # round trip (and occasionally making the agent mark an unexecuted step
        # complete). Events remain visible in the TUI and the result is injected
        # at the next iteration instead.
        return None

    def per_iteration(
        self,
        iteration: int,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or runtime_state is None:
            return None
        self._flush_events(runtime_state)
        return runtime_state.task_board.acceptance.take_notification() or None

    def post_tool(
        self,
        tool_name: str,
        tool_args: dict,
        result: ToolResult,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or runtime_state is None:
            return None
        acceptance = runtime_state.task_board.acceptance
        self._flush_events(runtime_state)
        if (
            tool_name == "update_plan_state"
            and str(tool_args.get("update_kind") or "").strip().lower() == "start"
            and str(tool_args.get("mode") or "").strip().lower() == "tracked"
            and result.status == "success"
        ):
            self._start_review(runtime_state)
            return None
        return acceptance.take_notification() or None

    def _start_review(self, runtime_state) -> None:
        acceptance = runtime_state.task_board.acceptance
        if not acceptance.revision or not acceptance.begin_review():
            return
        initial = acceptance.snapshot()
        task = runtime_state.task_board.original_task or runtime_state.task_board.goal
        plan_context = _plan_context(runtime_state)
        thread = threading.Thread(
            target=self._run_review,
            args=(runtime_state, task, plan_context, initial["checks"], initial["revision"]),
            name=f"acceptance-review-{runtime_state.session_id}",
            daemon=True,
        )
        thread.start()

    def _run_review(
        self,
        runtime_state,
        task: str,
        plan_context: dict,
        initial_checks: list[dict],
        initial_revision: int,
    ) -> None:
        acceptance = runtime_state.task_board.acceptance
        started = time.monotonic()
        last_error = ""
        self._emit(runtime_state, "started", {"attempts_allowed": self._max_attempts})
        for attempt in range(1, self._max_attempts + 1):
            attempt_started = time.monotonic()
            try:
                outcome = self._reviewer(
                    task=task,
                    plan_context=plan_context,
                    checks=initial_checks,
                    timeout_seconds=self._timeout_seconds,
                    previous_error=last_error or None,
                )
                if isinstance(outcome, ReviewOutcome):
                    self._queue_usage(runtime_state, outcome)
                    audit = outcome.raw
                else:
                    audit = outcome
                acceptance.snapshot()
                notification = _audit_notification(audit)
                if notification:
                    notification = (
                        "[SYSTEM] Fast plan audit:\n"
                        + notification
                        + "\n\nDecide whether to replan or update acceptance checks before continuing. "
                        "You own the final plan; do not blindly follow the audit."
                    )
                acceptance.finish_review(status="completed", notification=notification)
                final_snapshot = acceptance.snapshot()
                self._emit(
                    runtime_state,
                    "completed",
                    {
                        "attempt": attempt,
                        "attempt_elapsed_seconds": time.monotonic() - attempt_started,
                        "elapsed_seconds": time.monotonic() - started,
                        "before_checks": initial_checks,
                        "after_acceptance": final_snapshot,
                        "audit": _truncate_audit(audit),
                    },
                )
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._emit(
                    runtime_state,
                    "attempt_failed",
                    {
                        "attempt": attempt,
                        "elapsed_seconds": time.monotonic() - attempt_started,
                        "error": last_error,
                    },
                )

        warning = f"Fast plan audit failed open after {self._max_attempts} attempts: {last_error}"
        acceptance.finish_review(
            status="failed_open",
            warning=warning,
        )
        self._emit(
            runtime_state,
            "failed_open",
            {
                "elapsed_seconds": time.monotonic() - started,
                "error": last_error,
            },
        )

    @staticmethod
    def _emit(runtime_state, status: str, payload: dict) -> None:
        runtime_state.task_board.acceptance.queue_review_event(status, payload)

    @staticmethod
    def _flush_events(runtime_state) -> None:
        event_bus = getattr(runtime_state, "event_bus", None)
        if event_bus is None:
            return
        for item in runtime_state.task_board.acceptance.take_review_events():
            event_bus.emit(
                item["event_type"],
                agent="main_agent",
                payload=item["payload"],
            )

    @staticmethod
    def _queue_usage(runtime_state, outcome: ReviewOutcome) -> None:
        usage = dict(outcome.usage or {})
        if not usage:
            return
        cached_tokens = int(usage.get("cache_hit_tokens") or usage.get("cached_tokens") or 0)
        prompt_tokens = usage.get("prompt_tokens")
        cache_hit_ratio = 0.0
        if prompt_tokens:
            cache_hit_ratio = cached_tokens / int(prompt_tokens)
        runtime_state.task_board.acceptance.queue_runtime_event(
            "llm_usage",
            {
                "provider": outcome.provider,
                "model": outcome.model,
                "prompt_tokens": prompt_tokens,
                "cached_tokens": cached_tokens,
                "cache_hit_tokens": cached_tokens,
                "cache_miss_tokens": int(usage.get("cache_miss_tokens") or 0),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "cache_hit_ratio": cache_hit_ratio,
                "purpose": "acceptance_review",
            },
        )


def _plan_context(runtime_state) -> dict:
    board = runtime_state.task_board
    return {
        "mode": getattr(board, "planning_mode", ""),
        "goal": getattr(board, "goal", ""),
        "steps": list(getattr(board, "steps", []) or []),
        "current_step": getattr(board, "current_step", ""),
        "completed_steps": list(getattr(board, "completed_steps", []) or []),
        "next_action": getattr(board, "next_action", ""),
        "replan_reason": getattr(board, "replan_reason", ""),
    }


def _audit_notification(raw: object) -> str:
    text = _truncate_audit(raw)
    if not text:
        return ""
    return text


def _truncate_audit(raw: object, *, max_chars: int = 1500) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 24].rstrip() + "\n...[audit truncated]"


def _call_fast_reviewer(
    *,
    task: str,
    plan_context: dict | None = None,
    checks: list[dict],
    timeout_seconds: float,
    previous_error: str | None = None,
) -> ReviewOutcome:
    import json

    from ... import config
    from ...agent.providers import ProviderAdapter, get_client
    from ...agent.utils import _usage_to_dict

    profile = config.resolve_model_profile("fast")
    adapter = ProviderAdapter(profile.provider)
    client = get_client().with_options(timeout=timeout_seconds, max_retries=0)
    review_request = {
        "original_task": task,
        "start_plan": plan_context or {},
        "acceptance_checks": checks,
    }
    if previous_error:
        review_request["previous_invalid_response_error"] = previous_error
    response = client.chat.completions.create(
        **adapter.chat_kwargs(
            profile=profile,
            messages=[
                {
                    "role": "system",
                    "content": ACCEPTANCE_REVIEW_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(review_request, ensure_ascii=False),
                },
            ],
            max_tokens=1200,
        )
    )
    raw = response.choices[0].message.content or ""
    return ReviewOutcome(
        raw=raw,
        usage=_usage_to_dict(getattr(response, "usage", None)),
        provider=profile.provider,
        model=profile.model,
    )
