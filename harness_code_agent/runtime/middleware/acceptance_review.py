"""Asynchronous fast-model review for Terminal acceptance checks."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ...agent.acceptance import AcceptanceError
from .base import AgentMiddleware, MAIN_AGENT_NAMES


@dataclass(frozen=True)
class ReviewOutcome:
    raw: str
    usage: dict | None
    provider: str
    model: str


Reviewer = Callable[..., dict | ReviewOutcome]


class AcceptanceReviewMiddleware(AgentMiddleware):
    """Start review after planning and join it only before edits or finalization."""

    EDIT_TOOLS = {"write_file", "apply_patch"}

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
        is_final = (
            tool_name == "update_plan_state"
            and str(tool_args.get("update_kind") or "").strip().lower() == "final"
        )
        if tool_name not in self.EDIT_TOOLS and not is_final:
            return None
        acceptance = runtime_state.task_board.acceptance
        if acceptance.review_status == "not_started":
            return None
        acceptance.wait_for_review()
        self._flush_events(runtime_state)
        if is_final:
            notification = acceptance.take_notification()
            result_status = str(tool_args.get("result_status") or "").strip().lower()
            if notification and result_status == "success":
                return "[blocked] " + notification.removeprefix("[SYSTEM] ").strip()
        return None

    def post_tool(
        self,
        tool_name: str,
        tool_args: dict,
        result: str,
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
            and not result.startswith(("[error]", "[blocked]"))
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
        thread = threading.Thread(
            target=self._run_review,
            args=(runtime_state, task, initial["checks"], initial["revision"]),
            name=f"acceptance-review-{runtime_state.session_id}",
            daemon=True,
        )
        thread.start()

    def _run_review(self, runtime_state, task: str, initial_checks: list[dict], initial_revision: int) -> None:
        acceptance = runtime_state.task_board.acceptance
        started = time.monotonic()
        last_error = ""
        self._emit(runtime_state, "started", {"attempts_allowed": self._max_attempts})
        for attempt in range(1, self._max_attempts + 1):
            attempt_started = time.monotonic()
            try:
                outcome = self._reviewer(
                    task=task,
                    checks=initial_checks,
                    timeout_seconds=self._timeout_seconds,
                )
                if isinstance(outcome, ReviewOutcome):
                    self._queue_usage(runtime_state, outcome)
                    payload = _parse_json_object(outcome.raw)
                else:
                    payload = outcome
                changes = _validate_review_payload(payload)
                before_revision = acceptance.revision
                acceptance.apply_review_changes(
                    changes,
                    expected_revision=initial_revision,
                )
                snapshot = acceptance.snapshot()
                changed = snapshot["revision"] != before_revision
                notification = ""
                if changed:
                    notification = (
                        "[SYSTEM] Fast acceptance review updated the checklist. "
                        f"Use acceptance_revision={snapshot['revision']}. Latest checks: "
                        + json.dumps(snapshot["checks"], ensure_ascii=False, sort_keys=True)
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

        warning = f"Fast acceptance review failed open after {self._max_attempts} attempts: {last_error}"
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


def _validate_review_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or set(payload) != {"changes"}:
        raise AcceptanceError("review response must be an object containing only changes")
    changes = payload["changes"]
    if not isinstance(changes, list):
        raise AcceptanceError("review changes must be an array")
    for change in changes:
        if not isinstance(change, dict):
            raise AcceptanceError("each review change must be an object")
        kind = str(change.get("operation") or "").strip().lower()
        if kind not in {"add", "update"}:
            raise AcceptanceError("review operations must be add or update")
        if not str(change.get("reason") or "").strip():
            raise AcceptanceError("review changes require a reason")
        if kind == "add":
            required = {"text", "source", "verification_command"}
            if any(not str(change.get(field) or "").strip() for field in required):
                raise AcceptanceError("review add requires text, source, and verification_command")
        if kind == "update" and not str(change.get("id") or "").strip():
            raise AcceptanceError("review update requires an id")
    return changes


def _call_fast_reviewer(*, task: str, checks: list[dict], timeout_seconds: float) -> ReviewOutcome:
    from ... import config
    from ...agent.providers import ProviderAdapter, get_client
    from ...agent.utils import _usage_to_dict

    profile = config.resolve_model_profile("fast")
    adapter = ProviderAdapter(profile.provider)
    client = get_client().with_options(timeout=timeout_seconds, max_retries=0)
    response = client.chat.completions.create(
        **adapter.chat_kwargs(
            profile=profile,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Review a terminal coding agent's acceptance checklist against the original task. "
                        "Return JSON only: {\"changes\": [...]}. Changes may add missing checks or update "
                        "text, source, or verification_command of an existing check. Never delete checks. "
                        "Every change needs a concise reason. Added source must quote or closely paraphrase "
                        "the original task and be at most 300 characters. Suggest concrete, reasonable "
                        "verification commands; use \"manual\" only when command verification is impossible. "
                        "Return {\"changes\": []} when the checklist is complete."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"original_task": task, "acceptance_checks": checks},
                        ensure_ascii=False,
                    ),
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


def _parse_json_object(raw: str) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise AcceptanceError("review response must be a JSON object")
    return parsed
